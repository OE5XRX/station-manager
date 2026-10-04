"""TX-audio calibration resolution (spec 2026-10-03 §4).

One number per station: the agent limiter ceiling in dBFS, which maps to the target FM
deviation at that board's SA818. Unset or out-of-range values are clamped; the default errs
toward under-deviation so an uncalibrated station can never splatter.
"""

import math

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def effective_ceiling_dbfs(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CEILING_DEFAULT_DBFS
    if not math.isfinite(value):
        return CEILING_DEFAULT_DBFS
    return float(min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, value)))
