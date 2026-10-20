**Title:** feat(youtube): shuffle playlists

**Branch:** `factorsparsity:youtube-playlist-shuffle` → `anthonycaccese/240-MP:main`

---

## Summary

Adds a shuffle option to the YouTube module's playlists. Three files change:
`modules/youtube/manifest.json`, `views/Player.qml` and `views/Playlists.qml`.

- New `shuffle_playlists` setting (`Ask` / `Always` / `Never`, default `Ask`).
- With `Ask`, opening a playlist shows a Play-in-order / Shuffle prompt.
  `Always` and `Never` skip the prompt.
- Shuffled playback goes straight to `Player.qml`. It loads the playlist,
  shuffles it, and hands the queue to mpv as one playlist, so mpv advances on its
  own. Back from the player lands on the playlist list, not an item list.
- History and resume work exactly as before. `item` is re-pointed at each entry
  as mpv moves through the queue, so `savePosition` still records the video that
  actually played.

**Implementation note.** The queue goes to mpv as bare URLs after the first one,
not as the playlist URL with `--shuffle`. mpv expands a playlist URL through
yt-dlp up front, which took over three minutes on a Pi 4 before playback
started. Per-video URLs are resolved lazily as playback reaches each one, so the
first video starts as fast as it does today.

**Works alongside Autoplay Playlist Items.** This branch is rebased on the
`autoplay_playlist_items` change. The two don't interact:

- In shuffle mode, mpv advances through its own playlist. There is no
  `playlistIndex`, so `playNextPlaylistItem()` is a no-op.
- In-order playback has an empty shuffle queue, so autoplay behaves exactly as it
  does on `main`.

**Settings compatibility.** One new key. No existing keys are renamed, removed
or re-typed.

## AI involvement

The code was written with Claude Code, following the existing module's
patterns. I reviewed it and did all of the hardware testing below myself. This
description was drafted with Claude Code, and I edited it.

## Testing

- Platform(s) tested: Raspberry Pi 4B (1GB, arm64, Raspberry Pi OS Trixie),
  release build of this branch. Also a macOS source build.
- Display / output tested: CRT over composite, navigated with a USB remote.

What I tested:

- `Ask`, `Always` and `Never`, and navigating the prompt.
- Shuffle playback across a 90-video playlist.
- Backing out of the player.
- Re-tested on current `main` after the rebase, including in-order playback with
  Autoplay Playlist Items on.

Not tested on Windows, Steam Deck, or Pi 3/5.

## Checklist

- [ ] Changes work with remote-only navigation (up/down/left/right, enter, esc/backspace)
- [ ] Handled sizing and positioning using the `root.sh` & `root.sw` properties (did not hardcode pixels) and kept CRT overscan in mind
- [ ] Did not add tracking or analytics; only wrote settings to the local data directory if the change required storage
- [ ] Follows the patterns in [ARCHITECTURE.md](https://github.com/anthonycaccese/240-MP/blob/main/ARCHITECTURE.md) and the principles in [CONTRIBUTING.md](https://github.com/anthonycaccese/240-MP/blob/main/CONTRIBUTING.md)
