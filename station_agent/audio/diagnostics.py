"""Audio-path diagnostics: calibrated reference generation, dBFS metering, and
measured inject/tap pipelines for the TX chain (Spec: audio-path-diagnostics).

Pure DSP + argv builders are unit-tested; the subprocess capture/spawn seams are
injected (mirrors ``selftest.py``) so CI needs no GStreamer/PipeWire.

RF SAFETY: nothing here keys the SA818. Inject/measure end at the digital
ALSA/UAC2 edge (D); carrier keying is a separate control-plane action.
"""
from __future__ import annotations

import math
import struct

from station_agent.audio import selftest as _selftest

FULL_SCALE_S16 = 32767

REF_FREQ_HZ = 1000
REF_LEVEL_DBFS = -20.0
REF_WINDOW_MS = 300
REF_SETTLE_MS = 200


def rms_peak_dbfs(pcm: bytes) -> tuple[float | None, float | None, bool]:
    n = len(pcm) // 2
    if n == 0:
        return (None, None, True)
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    peak = max(abs(s) for s in samples)
    sumsq = sum(s * s for s in samples)
    rms = math.sqrt(sumsq / n)
    if peak == 0 or rms == 0:
        return (None, None, True)
    rms_dbfs = 20 * math.log10(rms / FULL_SCALE_S16)
    peak_dbfs = 20 * math.log10(peak / FULL_SCALE_S16)
    return (round(rms_dbfs, 2), round(peak_dbfs, 2), False)


def generate_sine_pcm(freq_hz: int, level_dbfs: float, duration_ms: int, rate: int) -> bytes:
    n = int(rate * duration_ms / 1000)
    amp = (10 ** (level_dbfs / 20)) * FULL_SCALE_S16
    out = [
        max(-32768, min(32767, int(round(amp * math.sin(2 * math.pi * freq_hz * i / rate)))))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *out)


MEAS_FD = 3
_RTP_PT = 96
_LOOPBACK = "127.0.0.1"


def build_measured_inject_argv(tx_node: str, freq_hz: int, level_dbfs: float, rate: int) -> list[str]:
    linear = 10 ** (level_dbfs / 20)
    return [
        "gst-launch-1.0", "-q",
        "audiotestsrc", "is-live=true", "wave=sine", f"freq={freq_hz}",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "audioconvert",
        "!", "volume", f"volume={linear:g}",
        "!", "opusenc", "audio-type=voice", "frame-size=20", "inband-fec=true",
        "!", "opusdec", "plc=true",
        "!", "audioconvert", "!", "audioresample",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "tee", "name=t",
        "t.", "!", "queue", "!", "pipewiresink", f"target-object={tx_node}", "sync=false",
        "t.", "!", "queue", "!", "audioconvert",
        "!", f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!", "fdsink", f"fd={MEAS_FD}",
    ]


def build_measured_tx_argv(tx_node: str, port: int, rate: int) -> list[str]:
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
    return [
        "gst-launch-1.0", "-q",
        "udpsrc", f"address={_LOOPBACK}", f"port={port}", f"caps={caps}",
        "!", "rtpjitterbuffer", "!", "rtpopusdepay",
        "!", "opusdec", "plc=true", "use-inband-fec=true",
        "!", "audioconvert", "!", "audioresample",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "tee", "name=t",
        "t.", "!", "queue", "!", "pipewiresink", f"target-object={tx_node}", "sync=false",
        "t.", "!", "queue", "!", "audioconvert",
        "!", f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!", "fdsink", f"fd={MEAS_FD}",
    ]


def build_reverse_tap_argv(tap: str, rate: int, duration: float) -> list[str]:
    return _selftest.build_tx_capture_argv(tap, rate, duration)
