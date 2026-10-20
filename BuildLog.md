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
| `test-integration` | Local test only: main + all three merged. Never PR this. Rebuild it after changing any feature branch. | |

Make changes on the feature branch, then `git checkout test-integration && git merge <branch>` to test together.

## Next steps

1. Exercise YouTube shuffle (prompt, Always/Never, mpv advancing).
2. Run Music TV on the Pi (backend, font in `~/.local/share/fonts`, bugs units).
3. Optionally squash `musictv-module`'s two commits into one before the PR.
4. Open three PRs: `factorsparsity:<branch>` → `anthonycaccese:main`.
5. Front panel: create `frontpanel-module` from the source repo's `segdisplay/`
   (plus the HDHomeRun `announceChannel` lines), merge into `test-integration`,
   and draft the aux-display `Main.qml` hook proposal as a separate PR.

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

**Not yet in the fork: the Front Panel module** (`segdisplay/`, 6 commits in the
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
