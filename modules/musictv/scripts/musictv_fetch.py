#!/usr/bin/env python3
"""Resolve the Music TV module's playlists into a flat cache the QML can read.

Reads two user files in 240-MP's data directory:

    musictv_playlists.txt   music videos  — playlist URLs, or single video URLs
    musictv_bumpers.txt     bumpers/idents — same format

Both use the same shape as youtube_playlists.txt: one entry per line, `#`
comments, and an optional `Display Name | <url>` prefix (kept for readability;
nothing downstream uses the name yet).

Writes musictv_cache.json:

    {"generated": <unix seconds>,
     "music":   [{"id","url","title","artist","song","album","label","director","duration"}],
     "bumpers": [ ... same ... ]}

Why a script and not the app: 240-MP modules with no C++ backend cannot spawn a
process from QML, and a YouTube playlist's RSS feed stops at 15 entries — far
too few for a rotation. yt-dlp --flat-playlist has no such limit, so the listing
happens out here and QML just reads the result.

Artist/song are parsed from the YouTube title ("Prince - When Doves Cry
(Official Video)"). Anything the parse cannot know — album, record label,
director — comes from an optional musictv_metadata.json keyed by video id:

    {"UG3VcCAlUgE": {"album": "Purple Rain", "label": "Warner Bros. Records"}}

Any field present there wins over the parsed value.

Env overrides: MUSICTV_DATA_DIR (default: 240-MP's data dir), YTDLP (path to yt-dlp).
"""

import json
import os
import re
import subprocess
import sys
import time

MAX_PLAYLIST_ITEMS = 500
FETCH_TIMEOUT = 300  # per playlist


def data_dir():
    env = os.environ.get("MUSICTV_DATA_DIR")
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "240-MP")
    return os.path.join(home, ".local", "share", "240-MP")


def find_ytdlp(root):
    """Same resolution order the app uses: data-dir drop-in, then PATH."""
    env = os.environ.get("YTDLP")
    if env:
        return env
    local = os.path.join(root, "yt-dlp")
    if os.access(local, os.X_OK):
        return local
    from shutil import which
    return which("yt-dlp")


# --- input files ------------------------------------------------------------

def read_entries(path):
    """-> [(name, url)] — blank lines and # comments dropped."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = ""
            if "|" in line:
                name, _, line = line.partition("|")
                name, line = name.strip(), line.strip()
            if line:
                out.append((name, line))
    return out


PLAYLIST_ID_RE = re.compile(r"[?&]list=([\w-]+)")
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})")


def classify(url):
    """-> ("playlist"|"video", canonical URL)."""
    m = PLAYLIST_ID_RE.search(url)
    if m:
        return "playlist", "https://www.youtube.com/playlist?list=" + m.group(1)
    m = VIDEO_ID_RE.search(url)
    if m:
        return "video", "https://www.youtube.com/watch?v=" + m.group(1)
    # A bare id: playlist ids are long and prefixed, video ids are 11 chars.
    if re.fullmatch(r"[\w-]{11}", url):
        return "video", "https://www.youtube.com/watch?v=" + url
    if re.fullmatch(r"[\w-]{12,}", url):
        return "playlist", "https://www.youtube.com/playlist?list=" + url
    return "playlist", url  # let yt-dlp decide


# --- title parsing ----------------------------------------------------------

# Bracketed groups that are production noise rather than part of the song name.
# "(Live)", "(Acoustic)" and the like are deliberately NOT in here — they are
# part of what the viewer is watching.
NOISE_RE = re.compile(
    r"[\(\[]\s*[^)\]]*\b("
    r"official|video|videoclip|audio|hd|hq|4k|remaster(?:ed)?|lyrics?|lyric|"
    r"mv|m/v|full|uncensored|explicit|clean|dirty|hq audio|music video|visualizer"
    r")\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)

SEP_RE = re.compile(r"\s+[-–—]\s+")


def clean_title(title):
    out = NOISE_RE.sub("", title)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" -–—|·")


def parse_artist_song(title, channel):
    """-> (artist, song). Falls back to the uploading channel as the artist."""
    cleaned = clean_title(title)
    parts = SEP_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist, song = parts[0].strip(), parts[1].strip()
    else:
        artist, song = (channel or "").strip(), cleaned
    song = song.strip().strip('"“”\'')
    # YouTube channels are routinely "Prince - Topic" or "PrinceVEVO"
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    artist = re.sub(r"VEVO$", "", artist)
    return artist.strip(), song


# --- yt-dlp -----------------------------------------------------------------

PRINT_FMT = "%(.{id,title,channel,uploader,playlist_title,duration})j"


def fetch(ytdlp, url, kind):
    args = [ytdlp, "--flat-playlist", "--no-warnings",
            "--print", PRINT_FMT]
    if kind == "playlist":
        args += ["-I", "1:%d" % MAX_PLAYLIST_ITEMS]
    args += ["--", url]
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after %ds" % FETCH_TIMEOUT, flush=True)
        return []
    if proc.returncode != 0 and not proc.stdout.strip():
        err = (proc.stderr or "").strip().splitlines()
        print("  FAILED: %s" % (err[-1] if err else "yt-dlp exit %d" % proc.returncode),
              flush=True)
        return []

    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = obj.get("id")
        title = (obj.get("title") or "").strip()
        # Tombstones YouTube leaves in place of removed entries. A flat listing
        # reports the named forms for some and an *empty* title for others
        # (terminated accounts) — both are unplayable, and each one left in
        # would stall the rotation while mpv discovers that for itself. 16 of
        # 91 entries in one real 80s playlist were of this kind.
        if not vid or not title or title in ("[Deleted video]", "[Private video]",
                                             "[Unavailable video]"):
            continue
        out.append(obj)
    return out


def build_pool(ytdlp, entries, overrides, pool_kind):
    """pool_kind is "music" or "bumper" — what the videos ARE. Not to be confused
    with the source kind below, which is how the *line* resolves (a playlist to
    expand, or a single video)."""
    pool, seen = [], set()
    for name, url in entries:
        source_kind, canonical = classify(url)
        print("%s (%s)%s" % (canonical, source_kind, "  [%s]" % name if name else ""), flush=True)
        raw = fetch(ytdlp, canonical, source_kind)
        print("  %d videos" % len(raw), flush=True)
        for obj in raw:
            vid = obj["id"]
            if vid in seen:
                continue
            seen.add(vid)
            channel = obj.get("channel") or obj.get("uploader") or ""
            artist, song = parse_artist_song(obj.get("title") or "", channel)
            item = {
                "id": vid,
                # The overlay script tells the two apart by this: a bumper gets
                # neither title card nor screen bug, however cleanly its title
                # happens to parse.
                "kind": pool_kind,
                "url": "https://www.youtube.com/watch?v=" + vid,
                "title": obj.get("title") or "",
                "artist": artist,
                "song": song,
                "album": "",
                "label": "",
                "director": "",
                "duration": obj.get("duration") or 0,
            }
            item.update({k: v for k, v in (overrides.get(vid) or {}).items()
                         if k in item})
            pool.append(item)
    return pool


def main():
    root = data_dir()
    ytdlp = find_ytdlp(root)
    if not ytdlp:
        sys.exit("yt-dlp not found (set YTDLP=/path/to/yt-dlp)")

    music_file = os.path.join(root, "musictv_playlists.txt")
    bump_file = os.path.join(root, "musictv_bumpers.txt")
    if not os.path.exists(music_file):
        sys.exit("no %s — create it with one playlist URL per line" % music_file)

    overrides = {}
    meta_file = os.path.join(root, "musictv_metadata.json")
    if os.path.exists(meta_file):
        try:
            with open(meta_file, encoding="utf-8") as fh:
                overrides = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print("ignoring unreadable musictv_metadata.json: %s" % exc, flush=True)

    print("=== music ===", flush=True)
    music = build_pool(ytdlp, read_entries(music_file), overrides, "music")
    print("=== bumpers ===", flush=True)
    bumpers = build_pool(ytdlp, read_entries(bump_file), overrides, "bumper")

    if not music:
        # Keeping a previous good cache beats replacing it with an empty one:
        # a transient network failure would otherwise take the channel off air.
        sys.exit("no music videos resolved — leaving any existing cache alone")

    cache = {"generated": int(time.time()), "music": music, "bumpers": bumpers}
    out_path = os.path.join(root, "musictv_cache.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=1)
    os.replace(tmp_path, out_path)
    print("wrote %s: %d music, %d bumpers" % (out_path, len(music), len(bumpers)),
          flush=True)


if __name__ == "__main__":
    main()
