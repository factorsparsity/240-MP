#!/usr/bin/env python3
"""
psip.py — dependency-free ATSC PSIP reader (no TSDuck).

Exists because TSDuck is not packaged for Raspbian/armhf, so the Pi could not
generate its own guide data. This reads the MPEG-TS straight off the tuner's
HTTP endpoint and parses the PSIP tables directly, using nothing but the
standard library — so it runs anywhere Python does.

Scope, and why it is small: ATSC A/65 allows Huffman-compressed text
(compression_type 0x01/0x02, Annex C tables). A probe of 732 real strings in
this market found **all** of them plain (compression_type 0x00, mode 0x00), so
the Huffman tables are deliberately not implemented — compressed segments are
skipped and counted rather than silently corrupting output. If a market turns
up that uses them, HUFFMAN_SKIPPED will be non-zero and that is the signal to
implement Annex C.

Structures implemented (ATSC A/65):
  MGT  0xC7  not parsed - we filter by table_id across every PID instead of
             following the MGT's PID redirections, which is simpler and was
             proven necessary: EIT lives on 0x1D00-0x1D17 and ETT on
             0x1E00-0x1E16 here, not the PSIP base PID 0x1FFB.
  TVCT 0xC8  source_id -> major.minor + short_name
  EIT  0xCB  per source_id: event_id, start_time (GPS), duration, title
  ETT  0xCC  etm_id -> long description; etm_id packs (source_id, event_id)
  STT  0xCD  gps_utc_offset, needed to convert GPS time to real UTC
"""

import struct
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

TID_TVCT, TID_CVCT, TID_EIT, TID_ETT, TID_STT = 0xC8, 0xC9, 0xCB, 0xCC, 0xCD

# Tables we actually parse.
WANTED_TIDS = {TID_TVCT, TID_CVCT, TID_EIT, TID_ETT, TID_STT}

# Every PSIP table_id (A/65): MGT, TVCT, CVCT, RRT, EIT, ETT, STT. Used only to
# decide whether a PID is PSIP-bearing and must NOT be blacklisted. This matters:
# the base PID 0x1FFB multiplexes MGT *and* TVCT *and* STT, so blacklisting it on
# first sight of an unwanted MGT silently loses the channel map and the clock —
# which is exactly the bug that made the first version return nothing.
PSIP_TIDS = {0xC7, 0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD}

# GPS epoch (1980-01-06) expressed as a Unix timestamp.
GPS_EPOCH_UNIX = 315964800

HUFFMAN_SKIPPED = 0   # non-zero means this market needs A/65 Annex C after all


# ---------------------------------------------------------------------------
# Transport stream -> PSI sections
# ---------------------------------------------------------------------------

def _mpeg_crc32(data):
    """MPEG-2 CRC-32 (poly 0x04C11DB7, non-reflected). Sections carry it as the
    last 4 bytes; checking it drops packets corrupted by weak signal instead of
    turning them into bogus programme entries."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


class Demuxer:
    """Incremental TS -> PSI section demuxer.

    Deliberately incremental rather than "read it all then parse": a 60s capture
    of a 19Mbps mux is ~140MB, and buffering that on a 426MB Pi while the player
    is running is a good way to get OOM-killed. State here is a few KB.

    Filters by table_id across all PIDs rather than following the MGT's PID
    redirections — necessary, since EIT/ETT are not on the PSIP base PID.
    """

    def __init__(self):
        self._bufs, self._want, self._skip = {}, {}, set()
        self._tail = b""

    def feed(self, chunk):
        """Feed raw bytes; yield complete, CRC-valid sections."""
        data = self._tail + chunk
        n = len(data) - (len(data) % 188)
        # Keep any partial trailing packet for the next chunk.
        self._tail = data[n:]
        yield from self._packets(data[:n])

    def _packets(self, data):
        bufs, want, skip = self._bufs, self._want, self._skip
        for i in range(0, len(data) - 187, 188):
            pkt = data[i:i + 188]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid in skip or pid == 0x1FFF:             # null packets
                continue
            afc = (pkt[3] >> 4) & 0x3
            idx = 4
            if afc & 0x2:                                # adaptation field
                idx += 1 + pkt[4]
            if not (afc & 0x1) or idx >= 188:
                continue
            payload = pkt[idx:]

            if pkt[1] & 0x40:                            # payload_unit_start_indicator
                ptr = payload[0]
                rest = payload[1 + ptr:]
                if len(rest) < 3:
                    continue
                if rest[0] not in PSIP_TIDS:
                    # Not PSIP at all (PES, PAT/PMT, …) — drop the whole PID.
                    if pid not in bufs:
                        skip.add(pid)
                    continue
                bufs[pid] = bytearray(rest)
                want[pid] = (((rest[1] & 0x0F) << 8) | rest[2]) + 3
            elif pid in bufs:
                bufs[pid] += payload

            if pid in bufs and len(bufs[pid]) >= want[pid]:
                sec = bytes(bufs[pid][:want[pid]])
                del bufs[pid], want[pid]
                # CRC over section+CRC is 0 for an intact section; this drops the
                # corruption a weak signal produces rather than emitting bad events.
                if sec[0] in WANTED_TIDS and len(sec) >= 4 and _mpeg_crc32(sec) == 0:
                    yield sec


def stream_sections(url, seconds, stop_when=None):
    """Yield PSI sections from a live tuner stream for up to `seconds`.

    Nothing is accumulated — chunks are demuxed and discarded as they arrive, so
    memory stays flat regardless of capture length. `stop_when` is an optional
    predicate called periodically; returning True ends the capture early (used by
    the RF probe, which only needs one VCT and should not tie up a tuner longer).
    """
    dmx = Demuxer()
    deadline = time.monotonic() + seconds
    with urllib.request.urlopen(url, timeout=30) as r:
        while time.monotonic() < deadline:
            try:
                chunk = r.read(188 * 1024)
            except OSError as e:
                # The tuner drops mid-capture occasionally (seen: ConnectionReset
                # after ~2min). Everything parsed up to here is valid, so end the
                # capture rather than raising and discarding a whole multiplex.
                print(f"    capture cut short: {type(e).__name__}", file=sys.stderr)
                return
            if not chunk:
                break
            yield from dmx.feed(chunk)
            if stop_when is not None and stop_when():
                return


# ---------------------------------------------------------------------------
# A/65 multiple_string_structure
# ---------------------------------------------------------------------------

def parse_multiple_string(buf, off, length):
    """Decode a multiple_string_structure to text (first language only).

    Segments within a string are concatenated — long descriptions are routinely
    split across several, and treating them separately truncates the text.
    """
    global HUFFMAN_SKIPPED
    end = min(off + length, len(buf))
    if off >= end:
        return ""
    nstr = buf[off]; off += 1
    out = []
    for _ in range(nstr):
        if off + 4 > end:
            break
        off += 3                                     # ISO_639_language_code
        nseg = buf[off]; off += 1
        parts = []
        for _ in range(nseg):
            if off + 3 > end:
                break
            comp, mode, nbytes = buf[off], buf[off + 1], buf[off + 2]
            off += 3
            seg = buf[off:off + nbytes]
            off += nbytes
            if comp == 0x00 and mode == 0x00:
                parts.append(seg.decode("latin-1", "replace"))
            elif comp == 0x00 and mode == 0x3F:      # UTF-16BE
                parts.append(seg.decode("utf-16-be", "replace"))
            else:
                HUFFMAN_SKIPPED += 1                 # A/65 Annex C, not implemented
        if parts:
            out.append("".join(parts))
        if out:
            break                                     # first language wins
    return "".join(out)


# ---------------------------------------------------------------------------
# Table parsers
# ---------------------------------------------------------------------------

def parse_stt(sec):
    """-> gps_utc_offset (seconds GPS runs ahead of UTC)."""
    # hdr(8) + protocol_version(1) + system_time(4) -> gps_utc_offset(1)
    return sec[13] if len(sec) > 13 else 0


def parse_vct(sec):
    """-> [ {source_id, number, name} ] for visible channels."""
    if len(sec) < 11:
        return []
    off = 9                                          # hdr(8) + protocol_version(1)
    num = sec[off]; off += 1
    channels = []
    for _ in range(num):
        if off + 32 > len(sec):
            break
        name = sec[off:off + 14].decode("utf-16-be", "replace").rstrip("\x00").strip()
        off += 14
        packed = (sec[off] << 16) | (sec[off + 1] << 8) | sec[off + 2]
        major = (packed >> 10) & 0x3FF
        minor = packed & 0x3FF
        off += 3
        off += 1 + 4 + 2 + 2                          # modulation, freq, tsid, program
        flags = (sec[off] << 8) | sec[off + 1]
        hidden = bool((flags >> 12) & 0x1)
        hide_guide = bool((flags >> 9) & 0x1)
        off += 2
        source_id = (sec[off] << 8) | sec[off + 1]; off += 2
        dlen = ((sec[off] & 0x03) << 8) | sec[off + 1]; off += 2
        off += dlen
        if not hidden and not hide_guide:
            channels.append({"source_id": source_id,
                             "number": f"{major}.{minor}",
                             "name": name})
    return channels


def parse_eit(sec):
    """-> (source_id, [ {event_id, start_gps, duration, title} ])."""
    if len(sec) < 11:
        return None, []
    source_id = (sec[3] << 8) | sec[4]               # table_id_extension
    off = 9
    num = sec[off]; off += 1
    events = []
    for _ in range(num):
        if off + 10 > len(sec):
            break
        event_id = ((sec[off] & 0x3F) << 8) | sec[off + 1]; off += 2
        start_gps = struct.unpack(">I", sec[off:off + 4])[0]; off += 4
        duration = ((sec[off] & 0x0F) << 16) | (sec[off + 1] << 8) | sec[off + 2]
        off += 3
        tlen = sec[off]; off += 1
        title = parse_multiple_string(sec, off, tlen); off += tlen
        if off + 2 > len(sec):
            break
        dlen = ((sec[off] & 0x0F) << 8) | sec[off + 1]; off += 2 + dlen
        events.append({"event_id": event_id, "start_gps": start_gps,
                       "duration": duration, "title": title})
    return source_id, events


def parse_ett(sec):
    """-> (source_id, event_id, text) or None for a channel ETM."""
    if len(sec) < 17:
        return None
    etm_id = struct.unpack(">I", sec[9:13])[0]
    if (etm_id & 0x3) != 0x2:                        # 0x2 = event ETM; 0x0 = channel
        return None
    source_id = etm_id >> 16
    event_id = (etm_id >> 2) & 0x3FFF
    text = parse_multiple_string(sec, 13, len(sec) - 13 - 4)   # minus CRC
    return source_id, event_id, text


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def scan_multiplex(url, seconds):
    """Capture one multiplex and return { "major.minor": [events] }.

    Event: {start, end, title, description} with ISO-8601 Z timestamps.
    """
    gps_offset, source_to_number, descriptions = 0, {}, {}
    events_by_source, seen = {}, set()

    for sec in stream_sections(url, seconds):
        tid = sec[0]
        if tid == TID_STT:
            gps_offset = parse_stt(sec)
        elif tid in (TID_TVCT, TID_CVCT):
            for ch in parse_vct(sec):
                source_to_number[ch["source_id"]] = ch["number"]
        elif tid == TID_EIT:
            sid, events = parse_eit(sec)
            for ev in events:
                key = (sid, ev["event_id"])
                # EIT-N windows overlap and every table cycles repeatedly, so the
                # same event arrives many times per capture.
                if key in seen:
                    continue
                seen.add(key)
                events_by_source.setdefault(sid, []).append(ev)
        elif tid == TID_ETT:
            got = parse_ett(sec)
            if got:
                sid, eid, text = got
                if text:
                    descriptions[(sid, eid)] = text

    guide = {}
    for sid, events in events_by_source.items():
        number = source_to_number.get(sid)
        if not number:
            continue                                  # EIT for a channel this VCT never described
        out = []
        for ev in events:
            start = datetime.fromtimestamp(
                ev["start_gps"] + GPS_EPOCH_UNIX - gps_offset, tz=timezone.utc)
            end = start + timedelta(seconds=ev["duration"])
            out.append({
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "title": ev["title"],
                "description": descriptions.get((sid, ev["event_id"]), ""),
            })
        out.sort(key=lambda e: e["start"])
        guide[number] = out
    return guide


def scan_channels(url, seconds):
    """Quick probe: which virtual channels does this RF carry? ({} = no signal).

    Returns as soon as a VCT arrives rather than holding the tuner for the full
    window — a sweep of 35 RF channels is otherwise needlessly slow.
    """
    for sec in stream_sections(url, seconds):
        if sec[0] in (TID_TVCT, TID_CVCT):
            chans = parse_vct(sec)
            if chans:
                return {c["number"]: c["name"] for c in chans}
    return {}
