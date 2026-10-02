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
