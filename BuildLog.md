# BuildLog — factorsparsity/240-MP fork

Working notes for Larry's contributions to upstream
[anthonycaccese/240-MP](https://github.com/anthonycaccese/240-MP). This file
lives only on the `worklog` branch of the fork so it never lands in an upstream
PR. Newest entry first.

## Repo layout

- `origin` = github.com/factorsparsity/240-MP (fork), `upstream` = anthonycaccese.
- `main` tracks `upstream/main`; keep it a pure mirror (fast-forward only).
- One branch per upstream PR, each rebased on `upstream/main`, one feature each:

| Branch | What | State |
|---|---|---|
| `hdhomerun-module` | New module: HDHomeRun live TV + ATSC PSIP guide. Pure QML (`HdhrService.qml`), Python EPG scanner + systemd units. | Tested on Mac (guide, tune) before the Aug 16 port; re-test crop memory and guide return. |
| `musictv-module` | New module: 80s music-video channel from YouTube playlists. `MusicTvBackend` (C++) + QML + mpv Lua overlay. | Tested on Mac. **Not yet on Pi** since the backend was added. |
| `youtube-playlist-shuffle` | Adds Play in order / Shuffle to the existing YouTube Playlists view + `shuffle_playlists` setting. Playlists themselves are upstream, not ours. | Builds; shuffle not yet exercised. |
| `frontpanel-module` | New module: Front Panel (SSD1306 OLED as 7-segment), `modules/segdisplay/`, plus HDHomeRun's `announceChannel` panel call. Based on `hdhomerun-module`. Needs the `Main.qml` hook (`scripts/patch_main_qml.py` at deploy). **Not a PR yet** — aux-display hook to be proposed upstream first. | Not tested since the port (Pi + OLED hardware only). |
| `test-integration` | Local test only: main + all three merged. Never PR this. Rebuild it after changing any feature branch. | |

Make changes on the feature branch, then `git checkout test-integration && git merge <branch>` to test together.

## Next steps

1. Exercise YouTube shuffle (prompt, Always/Never, mpv advancing).
2. Run Music TV on the Pi (backend, font in `~/.local/share/fonts`, bugs units).
3. Optionally squash `musictv-module`'s two commits into one before the PR.
4. Open three PRs: `factorsparsity:<branch>` → `anthonycaccese:main`.
5. Front panel: test on the Pi, then draft the aux-display hook proposal
   (generic "current screen / label / playback" from `Main.qml` or AppCore)
   as its own PR; rework `segdisplay` and `announceChannel` onto it.

## 2026-10-20 (evening) — deployed test-integration to the Pi

**Pi:** `vcr@240mp.local` (10.0.1.28; key `~/.ssh/id_ed25519_pi240mp`; the raw IP
fails host-key check — use the hostname). Pi 4, 1 GB, Debian 13, Qt 6.8 dev
packages present. Runs the `install.sh` layout: `/opt/240mp/{bin,share/240mp}`,
launched by `/usr/local/bin/240mp` from `240mp.service`.

**How it was deployed** (repeatable): clone the fork's `test-integration` to
`~/240-MP-src`; `cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=~/240mp-stage`;
`cmake --build build -j2` (~5 min; -j2 because 1 GB RAM); `cmake --install`;
run `modules/segdisplay/scripts/patch_main_qml.py` on the staged `Main.qml`;
swap `/opt/240mp` for the stage (previous kept as `/opt/240mp.bak-2026-10-20`);
`rm -rf ~/.cache/240-MP/qmlcache` (service has QML_FORCE_DISK_CACHE=1). Version
string is `dev`, so the in-app self-updater refuses to overwrite it. Disabled the
obsolete `musictv-fetch.{path,timer}`; all other module units already matched the
repo. yt-dlp drop-in at `~/.local/share/240-MP/bin/yt-dlp` (2026.08.19).
Rollback: swap the `.bak` back the same way.

**Gotcha — NEVER `systemctl stop/restart 240mp` as-is: it powers the Pi off.**
`ExecStopPost=/usr/local/bin/240mp-stop` runs `systemctl poweroff` for any exit
except 10 (terminal), 11 (update restart), 12 (reboot) — documented in
BUILDING.md, and it cost a physical power cycle. Safe pattern: drop-in
`/etc/systemd/system/240mp.service.d/99-deploy-no-poweroff.conf` with
`[Service]\nExecStopPost=`, `daemon-reload`, verify
`systemctl show 240mp -p ExecStopPost --value` is empty, do the work, then
remove the drop-in and `daemon-reload` (use a shell `trap` so it always comes off).
`segdisplay.service` can be restarted normally.

**Open: OLED not detected since that power cycle.** `segdisplay` reports
`/dev/i2c-1: Input/output error — running dry` from boot (before the deploy, with
the old build too); every earlier boot said `display connected at 0x3C`. Looks
like wiring/hardware. After reseating: `sudo i2cdetect -y 1` should show `3c`,
then `sudo systemctl restart segdisplay`.

## 2026-10-20 (later) — third repo found: Aug 16 work, front panel

**The original source repo is `~/Developer/hdhomerun-module`** (local only, no
remote; branch `qml-only-module`). The fork branches were cut from its commit
`90659b8 Prepare the three upstream submissions` (Aug 9). It also holds the
pre-written PR descriptions (`docs/pr-1…3.md`; pr-3 predates the Music TV
backend) and a 470-line BuildLog with the module's full history — read it before
deep HDHomeRun or front-panel work. Leave that repo as an archive; the fork is
now the single source.

**Ported to `hdhomerun-module`** (5 commits, original messages/authorship, paths
remapped to `modules/hdhomerun/`): return to the guide on the channel just
watched, forward all 8 OSC keys, per-channel crop memory (`scripts/hdhr-crop.lua`,
store `<dataRoot>/hdhr_crop.txt`) + its fix, and never run concurrent EPG scans
or save an empty one. One manual conflict: the Guide.qml hunk's context
contained front-panel `announceChannel` lines, which this branch does not carry.
Commit messages are the source repo's plain style, not `feat(hdhomerun):` —
squash before the PR if upstream wants one commit.

**Front Panel module, now on `frontpanel-module`** (`segdisplay/`, 6 commits in the
source repo). SSD1306 128x32 I2C OLED drawn as 7-segment digits: menu label >
setup > HDHomeRun channel > weather > elapsed > clock. Python daemon
(`segdisplay.py`, HTTP on 127.0.0.1:8377) + `SegHook.qml` + systemd unit. It
needs a hook in upstream `Main.qml` (there is no "current screen" signal); today
`patch_main_qml.py` patches it at deploy time. Plan: its own branch and its own
PR, proposing a generic aux-display hook in `Main.qml`/AppCore that any display
module can subscribe to — raise with the maintainer first.

**Decision: HDHomeRun's `announceChannel` (POST channel to :8377) stays with the
front panel**, not in the HDHomeRun PR. It is harmless when no panel exists
(fails silently, independent of Main.qml), but a hardcoded port for a module
upstream doesn't have would draw review pushback. `test-integration` merges
both, so a box with a panel loses nothing.

**Porting `frontpanel-module`.** Built on `hdhomerun-module`. The 5 segdisplay
commits were replayed with original messages/dates (`segdisplay/` →
`modules/segdisplay/`); the panel lines in 4 HDHomeRun views went into the first
commit, copied from the source repo's final versions after verifying they
differed from the branch *only* by those lines. Result is byte-identical to the
source repo's `segdisplay/` and views. `deploy-segdisplay.sh` resolves paths
relative to itself, so it still works; the HDHomeRun views themselves are
deployed by the personal `deploy-to-pi.sh` (backup folder / source repo).

## 2026-10-20 — consolidation, Music TV backend, font fix

**How the work got split.** Work started uncommitted in the upstream clone
(`~/Developer/240-MP`: `modules/mtv`, `modules/hdhomerun` + a C++ HDHomeRun
backend), then was cleaned up and committed in a second clone
(`~/Developer/240-MP-fork`) as the three branches above. The fork versions are
the newer ones: mtv → musictv rename, MTV logos and Kabel font removed for
licensing, HDHomeRun moved from C++ to pure QML, `/home/vcr` paths templated
(`@USER@`/`@MODULE_DIR@`). Both clones now track the same fork branches.
Old loose copies (including personal Pi scripts `deploy-to-pi.sh`,
`build-240mp-pi-zero.sh`, `240mp-kiosk.service` found nowhere else) are backed
up at `~/Developer/240-MP-local-backup-2026-10-20/`; the old C++ HDHomeRun
wiring is `stash@{0}` in `~/Developer/240-MP`.

**Rebase onto upstream (35 commits).** Only conflict: `modules/youtube/manifest.json`,
upstream's new `autoplay_playlist_items` next to our `shuffle_playlists` — kept
both. They don't interact: shuffle mode has no `playlistIndex`, so autoplay's
`playNextPlaylistItem()` returns false; in-order mode has an empty queue.

**Music TV: script → C++ backend.** The module was pure QML, and QML can't spawn
processes, so listing playlists (`yt-dlp --flat-playlist`; RSS stops at 15
entries) lived in `musictv_fetch.py` run by systemd. Off the Pi that meant
editing a playlist did nothing until the script ran by hand. YouTubeBackend
already does the same listing in-app, so `MusicTvBackend` copies its QProcess
pattern. Rebuilds the cache only when an input file is newer or the cache is
>24h old; on failure falls back to the old cache. `musictv_cache.json` must
stay on disk — `musictv-overlay.lua` reads it inside mpv. Title parsing was
ported 1:1; verified identical output to the Python on 631 videos. Music TV no
longer needs `QML_XHR_ALLOW_FILE_READ` (HDHomeRun still does).
`musictv_bugs.py` (PNG → raw BGRA for mpv `overlay-add`) is still Python;
moving it into the backend is a possible follow-up.

**Gotcha — title font never loaded on macOS.** `Player.qml` passed
`--osd-fonts-dir`, but `MpvController` appends its own (for the OSC's VCR font)
*after* `extraArgs`, and mpv keeps only the last. Can't pass both. Fix: fonts go
in the user font dir (`~/Library/Fonts` on Mac, `~/.local/share/fonts` +
`fc-cache` on Pi); CoreText/fontconfig find them. New `title_font` setting:
Josefin Sans (shipped, OFL) or Kabel (user's own copy — commercial, can't ship).
Check what mpv picked: `grep fontselect $TMPDIR/240mp-mpv.log`.

**Gotcha — "only bumpers play" / YouTube "PLAYBACK FAILED" = stale yt-dlp.**
Homebrew yt-dlp 2026.07.04 got HTTP 403 on official label/VEVO uploads while
fan-uploaded bumpers still streamed, so it looked like yt-dlp worked. In an mpv
playlist a failed entry is skipped silently, so Music TV showed only bumpers.
2026.08.19 fixed it. Use the drop-in `<dataRoot>/bin/yt-dlp` (app prefers it
over PATH, see `src/util/YtDlpLocator.h`) and `yt-dlp -U` it. On the Pi the apt
version will be worse — use the drop-in there too.

**Gotcha — screen bugs/Kabel "missing" is by design.** None ship (trademark /
licence). User drops PNGs in `<dataRoot>/musictv_bugs/` and runs
`musictv_bugs.py`. PNGs need real transparency; the converter warns otherwise
(`screenbug1.png` in the backup is opaque).

**Running on macOS.** Upstream now builds a bundle:
`APP_ROOT=$(pwd) QML_XHR_ALLOW_FILE_READ=1 ./build/240mp.app/Contents/MacOS/240mp`
(CLAUDE.md's `./build/240mp` is stale). QML is read live from the repo, so a
QML-only change needs an app restart, not a rebuild. mpv log:
`$TMPDIR/240mp-mpv.log`.

**User data (Mac, `~/Library/Application Support/240-MP/`).** `youtube_playlists.txt`,
`youtube_subscriptions.txt`, `musictv_playlists.txt`, `musictv_bumpers.txt`,
`musictv_bugs/`, `bin/yt-dlp`. Line format for all list files:
`[Name | ]<URL or ID>`, `#` comments.
