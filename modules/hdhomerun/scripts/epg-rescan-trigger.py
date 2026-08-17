#!/usr/bin/env python3
"""Run an EPG scan when the app asks for one.

Why this exists: the module has no C++ backend, and 240-MP contains no QProcess
at all — nothing QML-callable can launch a process. So the in-app "Rescan Guide
Data" control cannot run the scanner directly. What QML *can* do is write a
setting (appCore.save_setting), so the toggle sets a flag in config.json, a
systemd .path unit notices the file change, and this script does the actual work.

The spawn lives in systemd, which is where the nightly timer already is.

Loop safety: clearing the flag rewrites config.json, which retriggers the .path
unit and runs this script a second time. That run sees the flag already off,
writes nothing, and exits — so it settles after exactly one no-op rather than
looping. Every other settings change lands here too and exits the same way.
"""

import fcntl
import json
import os
import pathlib
import subprocess
import sys

MODULE_ID = "com.240mp.hdhomerun"
KEY = "rescan_guide"

# One scan at a time, across both entry points. systemd serialises each *unit*,
# but epg-scan.service (nightly) and epg-rescan.service (the in-app toggle) are
# two units, and the tuner is one device: when the timer fired during a running
# rescan, every RF came back "HTTP 503 Service Unavailable", the whole run took
# a second, and it still rewrote the cache — leaving an hour-old timestamp on
# day-old data, which is a genuinely confusing thing to debug from the guide.
LOCK_PATH = pathlib.Path(os.environ.get("EPG_SCAN_LOCK", "/tmp/240mp-epg-scan.lock"))

CONFIG = pathlib.Path(
    os.environ.get("MP240_CONFIG", pathlib.Path.home() / ".local/share/240-MP/config.json")
)
SCANNER = pathlib.Path(
    os.environ.get("EPG_SCANNER", pathlib.Path(__file__).with_name("epg_scanner.py"))
)


def is_on(value):
    # Toggles arrive as the manifest's "ON"/"OFF" strings, but a JSON bool is
    # cheap to accept and avoids a silent no-op if that ever changes.
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("ON", "TRUE", "1")


def main():
    # --force runs the scan regardless of the flag. Used by epg-scan.timer, so
    # that the scheduled scan and the in-app one share this single entry point —
    # and therefore both pick up device_id below, rather than the timer quietly
    # scanning whatever tuner was hardcoded at deploy time.
    force = "--force" in sys.argv[1:]

    if not CONFIG.exists():
        return 0
    try:
        cfg = json.loads(CONFIG.read_text())
    except json.JSONDecodeError:
        # The app may be mid-write. The next change retriggers us.
        print(f"config.json not parseable yet, skipping", file=sys.stderr)
        return 0

    module = cfg.get("modules", {}).get(MODULE_ID, {})
    requested = is_on(module.get(KEY))
    if not (requested or force):
        return 0

    # Clear BEFORE scanning, not after: a scan takes ~40-90 minutes, and leaving
    # the flag set for that long means any unrelated settings change retriggers a
    # second concurrent scan. (systemd also serialises via the service, but the
    # flag is the thing the user sees, and it should return to OFF immediately.)
    if requested:
        module[KEY] = "OFF"
        CONFIG.write_text(json.dumps(cfg, indent=2))
        print("rescan requested; flag cleared, starting scan")
    else:
        print("scheduled scan starting")

    if not SCANNER.exists():
        print(f"scanner not found at {SCANNER}", file=sys.stderr)
        return 1

    # Held for the whole scan; released when this process exits, however it exits.
    # Non-blocking on purpose: a scan that cannot start now would be starting
    # against a busy tuner anyway, and the timer will come round again.
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another scan is already running; skipping this one", file=sys.stderr)
        return 0

    # Scan the tuner the user actually selected. The scanner reads HDHOMERUN_IP
    # and otherwise falls back to a hardcoded default, so without this the module
    # setting and the scan target are two independent settings that drift apart
    # silently the moment someone switches tuners.
    env = dict(os.environ)
    device = str(module.get("device_id") or "").strip()
    if device:
        env["HDHOMERUN_IP"] = device
        print(f"scanning tuner {device}")
    else:
        print("no device_id set; scanner will use its own default", file=sys.stderr)

    result = subprocess.run([sys.executable, str(SCANNER)], env=env)
    print(f"scan finished with exit {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
