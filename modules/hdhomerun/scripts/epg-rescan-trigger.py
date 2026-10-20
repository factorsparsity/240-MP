#!/usr/bin/env python3
"""Run one EPG scan against the tuner selected in 240-MP's settings.

The single entry point for both ways a scan starts: the "Rescan Guide Data"
settings action (HDHomeRunBackend::rescan_guide) and the optional nightly
epg-scan.timer. Sharing it means both scan the configured device_id and both
take the same lock.
"""

import fcntl
import json
import os
import pathlib
import subprocess
import sys

MODULE_ID = "com.240mp.hdhomerun"

# One scan at a time, across both entry points: the nightly timer and the
# settings action are independent, and the tuner is one device: when the timer fired during a running
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


def main():
    # Older epg-scan.service units pass --force; it is accepted and ignored.
    module = {}
    if CONFIG.exists():
        try:
            module = json.loads(CONFIG.read_text()).get("modules", {}).get(MODULE_ID, {})
        except json.JSONDecodeError:
            # The app may be mid-write; scan with the scanner's own default.
            print("config.json not parseable, ignoring device_id", file=sys.stderr)
    print("scan starting")

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
