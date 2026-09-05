#!/usr/bin/env python3
"""Regenerate ``media_frame_slot0rx.bin`` — a §5.3 media frame wrapping ONE real Opus
packet of a 1 kHz tone (Spec 0 §5.7).

Both Session B (agent) and Session C (server) assert against this golden file: header
parse (both) and round-trip Opus decode → 1 kHz FFT peak (wherever an Opus decoder is
available). It is committed as a binary so no build step needs libopus/GStreamer.

Reproduce (needs a scratch venv with PyAV, which bundles libopus/ffmpeg — NOT a runtime
or CI dependency of station_agent):

    python3 -m venv /tmp/opusgen && /tmp/opusgen/bin/pip install av numpy
    /tmp/opusgen/bin/python tests/fixtures/audio/gen_media_frame.py

The wire frame uses stream_ref=0 (slot0.rx), seq=0, ts=0, flags=0.
"""

import math
import pathlib
import sys

import av  # type: ignore

# Import the production packer so the fixture stays byte-identical to what the agent emits.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from station_agent.audio import frame  # noqa: E402

RATE = 8000
TONE_HZ = 1000
FRAME_SAMPLES = RATE * 20 // 1000  # 20 ms @ 8 kHz = 160 samples
OUT = pathlib.Path(__file__).with_name("media_frame_slot0rx.bin")


def _tone_s16(n: int) -> bytes:
    out = bytearray()
    for i in range(n):
        v = int(20000 * math.sin(2 * math.pi * TONE_HZ * i / RATE))
        out += int(v).to_bytes(2, "little", signed=True)
    return bytes(out)


def main() -> int:
    cc = av.CodecContext.create("libopus", "w")
    cc.sample_rate = RATE
    cc.format = "s16"
    cc.layout = "mono"
    cc.options = {"application": "voip", "frame_duration": "20", "vbr": "on"}

    # Feed a couple of frames so libopus is primed, then keep the first emitted packet.
    packet_bytes = None
    for _ in range(4):
        arr = _tone_s16(FRAME_SAMPLES)
        af = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        af.sample_rate = RATE
        af.planes[0].update(arr)
        for pkt in cc.encode(af):
            if packet_bytes is None and bytes(pkt):
                packet_bytes = bytes(pkt)
    for pkt in cc.encode(None):  # flush
        if packet_bytes is None and bytes(pkt):
            packet_bytes = bytes(pkt)

    if not packet_bytes:
        print("ERROR: encoder produced no Opus packet", file=sys.stderr)
        return 1

    data = frame.pack_frame(stream_ref=0, seq=0, ts=0, flags=0, payload=packet_bytes)
    OUT.write_bytes(data)
    print(f"wrote {OUT} ({len(data)} bytes; opus payload {len(packet_bytes)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
