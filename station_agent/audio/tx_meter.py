"""TX modulation meter (spec §3.5).

The meter taps the chain right BEFORE the limiter (F32, so nothing has clipped yet) and
runs the samples through the limiter's exact static curve. ``audiodynamic`` is memoryless,
so this reproduces the post-limiter samples (the level at D — the sink is unity since
linux-image #99) AND yields the true limiter gain reduction from one tap. Final F32->S16
conversion clips at full scale; modelled as ``min(|y|, 1.0)``.

Ratio convention: ``limiter_ratio`` here is the human N:1 value from ``TxDspPolicy``
(e.g. 1000.0), so the static curve is ``y = t + (a - t) / N``. The gst argv instead emits
the gst-native ``ratio=1/N`` (gst computes ``thr + (x - thr) * ratio``) — same curve.
Never pass the gst-native value here.
"""

from __future__ import annotations

import math
import struct

METER_HZ = 8
_LIMITING_DB = 0.5


def chunk_bytes(rate: int) -> int:
    return (rate // METER_HZ) * 4


def _db(x: float) -> float | None:
    return round(20.0 * math.log10(x), 1) if x > 0 else None


def compute_meter(
    pcm_f32: bytes, *, limiter_threshold: float | None, limiter_ratio: float
) -> dict:
    n = len(pcm_f32) // 4
    pre_peak = post_peak = 0.0
    acc = 0.0
    count = 0
    t = limiter_threshold
    for (x,) in struct.iter_unpack("<f", pcm_f32[: n * 4]):
        if not math.isfinite(x):
            continue
        a = abs(x)
        y = a if (t is None or a <= t) else t + (a - t) / limiter_ratio
        y = min(y, 1.0)
        pre_peak = max(pre_peak, a)
        post_peak = max(post_peak, y)
        acc += y * y
        count += 1
    if post_peak <= 0.0:
        return {"peak_dbfs": None, "rms_dbfs": None, "gain_reduction_db": 0.0, "limiting": False}
    gr = 0.0
    if t is not None and pre_peak > t:
        gr = round(20.0 * math.log10(min(pre_peak, 1e6) / post_peak), 1)
    return {
        "peak_dbfs": _db(post_peak),
        "rms_dbfs": _db(math.sqrt(acc / count)),
        "gain_reduction_db": max(0.0, gr),
        "limiting": gr >= _LIMITING_DB,
    }
