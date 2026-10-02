"""Generate the U-anchor Opus reference fixture.

Produces ``apps/audio/data/ref_1khz_-20dbfs_16k.opusframes``: a sequence of
length-prefixed raw Opus packets encoding a 1 kHz sine wave at exactly
−20 dBFS peak, 20 ms per packet, 16 kHz mono voice.

The exact GStreamer pipeline used:

    gst-launch-1.0 -q \\
        audiotestsrc num-buffers=25 wave=sine freq=1000 volume=1.0 \\
        ! audio/x-raw,rate=16000,channels=1 \\
        ! audioconvert \\
        ! volume volume=0.1 \\
        ! opusenc audio-type=voice frame-size=20 inband-fec=true \\
        ! rtpopuspay pt=96 \\
        ! udpsink host=127.0.0.1 port=47900 sync=false

``audiotestsrc`` has a built-in ``volume`` property that defaults to 0.8; without
explicitly setting it to 1.0, the tone would be at (0.8 × 0.1) = 0.08 of full
scale ≈ −21.94 dBFS instead of the intended −20 dBFS.  The ``volume volume=0.1``
element is the sole gain stage; ``audiotestsrc volume=1.0`` passes through at unity.

num-buffers=25 → approximately 25 RTP packets of 20 ms each ≈ 500 ms of audio.

File format:
    Concatenated records, each: ``struct.pack(">H", len(opus_payload)) + opus_payload``
    (big-endian uint16 length prefix, then raw Opus bytes).

This script is idempotent: re-running it overwrites the fixture.
It is NOT run in CI — the fixture is committed as a binary artifact.

Usage::

    python3 scripts/gen_audio_reference.py

Requires: gst-launch-1.0 with audiotestsrc, opusenc, rtpopuspay, udpsink.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
from pathlib import Path

# Ensure the project root is on sys.path so station_agent is importable when
# this script is run directly (e.g. ``python3 scripts/gen_audio_reference.py``
# from the project root).
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from station_agent.audio.rtp import strip_rtp  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUT_PATH = (
    Path(__file__).parent.parent / "apps" / "audio" / "data" / "ref_1khz_-20dbfs_16k.opusframes"
)
UDP_PORT = 47900
NUM_BUFFERS = 25
RECV_TIMEOUT = 2.0  # seconds; stop when no datagram arrives within this window

GST_PIPELINE = [
    "gst-launch-1.0",
    "-q",
    "audiotestsrc",
    f"num-buffers={NUM_BUFFERS}",
    "wave=sine",
    "freq=1000",
    "volume=1.0",  # override audiotestsrc's 0.8 default; only the volume element sets the level
    "!",
    "audio/x-raw,rate=16000,channels=1",
    "!",
    "audioconvert",
    "!",
    "volume",
    "volume=0.1",
    "!",
    "opusenc",
    "audio-type=voice",
    "frame-size=20",
    "inband-fec=true",
    "!",
    "rtpopuspay",
    "pt=96",
    "!",
    "udpsink",
    "host=127.0.0.1",
    f"port={UDP_PORT}",
    "sync=false",
]


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Bind the UDP socket BEFORE spawning gst so no packets are missed.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", UDP_PORT))
    sock.settimeout(RECV_TIMEOUT)

    print(f"Bound UDP socket on 127.0.0.1:{UDP_PORT}")
    print("Spawning GStreamer pipeline …")

    proc = subprocess.Popen(
        GST_PIPELINE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    packets: list[bytes] = []
    try:
        while True:
            try:
                datagram, _ = sock.recvfrom(65536)
            except TimeoutError:
                # No packet within the timeout window — pipeline has finished.
                break

            # Strip RTP header to get the raw Opus payload.
            try:
                opus_payload = strip_rtp(datagram)
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] strip_rtp failed: {exc}", file=sys.stderr)
                continue

            if opus_payload:
                packets.append(opus_payload)
    finally:
        sock.close()
        proc.wait(timeout=5)

    if not packets:
        print(
            "ERROR: no packets received — check that gst-launch-1.0 is installed.", file=sys.stderr
        )
        sys.exit(1)

    # Write length-prefixed records.
    with OUT_PATH.open("wb") as fh:
        for payload in packets:
            fh.write(struct.pack(">H", len(payload)))
            fh.write(payload)

    print(f"Wrote {len(packets)} Opus packets to {OUT_PATH}")


if __name__ == "__main__":
    main()
