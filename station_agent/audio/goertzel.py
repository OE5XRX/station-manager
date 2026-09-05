"""Goertzel single-frequency power detector — pure Python, no numpy.

The audio selftest (Spec 0 §7/§8) proves a known tone survives the RX/TX Opus roundtrip by
asserting an FFT peak at a known frequency. numpy is not an image dependency, so we use the
Goertzel algorithm — an O(N) single-bin DFT that Spec 0 §8 already names for the TX probe.
It answers exactly the question the selftest asks ("is frequency f present and dominant?")
without pulling in a full FFT stack.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def goertzel_power(samples: Sequence[float], target_hz: float, rate_hz: float) -> float:
    """Return the (normalized) power of ``target_hz`` in ``samples``.

    ``samples`` may be floats or ints (raw S16 PCM works directly). The result is
    normalized by sample count so thresholds are independent of block length.
    """
    n = len(samples)
    if n == 0 or rate_hz <= 0:
        return 0.0
    k = int(0.5 + (n * target_hz) / rate_hz)
    omega = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples:
        s = float(sample) + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return power / (n * n)


def dominant_bin(samples: Sequence[float], candidates: Sequence[float], rate_hz: float) -> float:
    """Return the candidate frequency with the highest Goertzel power."""
    return max(candidates, key=lambda f: goertzel_power(samples, f, rate_hz))
