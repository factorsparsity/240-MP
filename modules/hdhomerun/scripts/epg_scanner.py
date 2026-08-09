#!/usr/bin/env python3
"""
epg_scanner.py — PSIP/EIT guide scanner for the HDHomeRun 240-MP module.

Uses `psip.py`, a dependency-free ATSC parser in this same directory. It used to
shell out to TSDuck; that was replaced because **TSDuck is not packaged for
Raspbian/armhf**, which meant the Pi could not generate its own guide data. Now
the only requirement is Python itself, so this runs on the Pi, the Mac, or
anywhere else unchanged.

Two things learned the hard way, both still true and both easy to get wrong:

1. **You cannot scan PSIP from lineup.json's URL.** lineup.json hands you
   `http://<ip>:5004/auto/v2.1`, which makes the tuner remux down to that single
   programme — the stream then contains only PAT and PMT, no PSIP whatsoever.
   Guide data requires tuning the PHYSICAL RF channel:
   `http://<ip>:5004/auto/ch<RF>`, which carries the whole ~19Mbps multiplex.

2. **Virtual channel majors do not map to physical RF channels.** In this market
   9.4/9.5/9.6 are on RF 9 while 9.1/9.2/9.3 are on RF 29; 5.x is split across
   RF 30 and 35. So the RF map cannot be guessed from GuideNumber — it has to
   come from tuning each RF and reading its VCT. That sweep is slow, so it is
   cached in the output file and reused.

Run on a systemd timer (see epg-scan.{service,timer}). Each capture ties up one
tuner for CAPTURE_SECONDS.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psip  # noqa: E402

# 240-MP's own data dir on Linux, which is also where HdhrService.load_guide()
# reads from — so no separate path to keep in sync, and no root needed. On macOS
# the data dir is ~/Library/Application Support/240-MP, so override
# EPG_CACHE_PATH there.
CACHE_PATH = Path(os.environ.get(
    "EPG_CACHE_PATH", Path.home() / ".local/share/240-MP/epg_cache.json"))

CONFIG_PATH = Path(os.environ.get(
    "MP240_CONFIG", Path.home() / ".local/share/240-MP/config.json"))
MODULE_ID = "com.240mp.hdhomerun"


def resolve_tuner_ip():
    """Which tuner to scan: HDHOMERUN_IP, else the app's own device_id setting.

    There used to be a hardcoded "10.0.1.94" fallback here. That was wrong: on any
    other network it silently scanned a stranger's address and reported confusing
    failures rather than saying what was actually missing. It also meant the
    scanner and the module's device_id were two independent settings that drifted
    apart the moment someone switched tuners in the UI.

    Reading config.json makes the app's setting the single source of truth, and
    keeps the scanner runnable standalone with no environment at all.
    """
    env_ip = os.environ.get("HDHOMERUN_IP", "").strip()
    if env_ip:
        return env_ip

    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        ip = str(cfg["modules"][MODULE_ID].get("device_id") or "").strip()
        if ip:
            print(f"using tuner {ip} from {CONFIG_PATH}")
            return ip
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    sys.exit(
        "No tuner configured. Set HDHOMERUN_IP=<tuner ip>, or pick a tuner in the\n"
        f"app (press D in the channel list), which writes device_id to {CONFIG_PATH}."
    )


HDHOMERUN_IP = resolve_tuner_ip()

# Per multiplex. 45s was NOT enough and the shortfall was invisible without a
# side-by-side comparison: on a dense mux (RF 32 here carries 7 virtual channels,
# so PSIP bandwidth is split 7 ways) the near-term EIT windows arrive fine while
# the later EIT-N tables and most ETT descriptions never cycle. Measured on RF 32:
# 45s -> 4.3 had 108 events / 4.4 had 3; 150s -> 144 and 32, matching a reference
# capture almost exactly, with descriptions going from missing to complete.
# Cost is real — 15 multiplexes x 150s is ~40min — but this runs on a timer.
CAPTURE_SECONDS = 150
PROBE_SECONDS = 10        # per RF when sweeping; returns early once a VCT lands

# US ATSC after the repack: RF 2-36 (37 is radio astronomy, 38+ reallocated).
RF_CHANNELS = range(2, 37)


def stream_url(rf):
    return f"http://{HDHOMERUN_IP}:5004/auto/ch{rf}"


def discover_multiplexes():
    """Sweep the RF band. -> { "rf": [GuideNumber, ...] } for channels with signal.

    The result gets cached and reused indefinitely, so a transient failure here is
    expensive: an RF that errors is simply absent from the map and never scanned
    again. Both tuners being briefly busy (HTTP 503) is enough to do it. Hence the
    retry, and the warning at the end.
    """
    found, failed = {}, []
    for rf in RF_CHANNELS:
        for attempt in (1, 2):
            try:
                chans = psip.scan_channels(stream_url(rf), PROBE_SECONDS)
                break
            except Exception as e:
                err = e
                chans = None
        if chans is None:
            print(f"  RF {rf}: {err}", file=sys.stderr)
            failed.append(rf)
            continue
        if chans:
            found[str(rf)] = sorted(chans)
            print(f"  RF {rf}: {', '.join(sorted(chans))}", flush=True)
    if failed:
        print(f"WARNING: {len(failed)} RF channels errored twice and are missing "
              f"from the cached map: {failed}. Delete the cache to re-sweep.",
              file=sys.stderr)
    return found


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file so a crash mid-write cannot leave the app reading a
    # truncated cache.
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(CACHE_PATH)


def main():
    cache = load_cache()

    rf_map = cache.get("_multiplexes")
    if not rf_map:
        print("No cached multiplex map; sweeping RF 2-36 (one-time)...", flush=True)
        rf_map = discover_multiplexes()
        if not rf_map:
            print("No ATSC signal found on any RF channel.", file=sys.stderr)
            return 1

    guide = cache.get("channels", {})
    for rf in sorted(rf_map, key=int):
        print(f"Scanning RF {rf} ({', '.join(rf_map[rf])})...", flush=True)
        try:
            guide.update(psip.scan_multiplex(stream_url(rf), CAPTURE_SECONDS))
        except Exception as e:
            print(f"  failed on RF {rf}: {e}", file=sys.stderr)

    save_cache({"_multiplexes": rf_map, "channels": guide})
    total = sum(len(v) for v in guide.values())
    print(f"Wrote {total} events across {len(guide)} channels to {CACHE_PATH}")
    if psip.HUFFMAN_SKIPPED:
        # Only fires in a market that compresses PSIP text; see psip.py.
        print(f"WARNING: skipped {psip.HUFFMAN_SKIPPED} Huffman-compressed strings "
              f"(A/65 Annex C not implemented) — some titles/descriptions will be "
              f"missing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
