"""Sanitize agent -> browser TX modulation-meter frames (spec §3.5).

The agent is semi-trusted (Ed25519-authenticated device, but its frames reach every
operator's browser): whitelist keys, coerce types, bound numbers.
"""

import math

# "failed" = the TX pipeline died even without DSP.
_DSP_MODES = {"off", "full", "degraded", "failed"}


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(min(hi, max(lo, v)))


def sanitize(msg):
    """Return a wire-safe tx_meter dict, or None if the frame is unroutable."""
    if not isinstance(msg, dict):
        return None
    slot = msg.get("slot")
    # No existing slot constant in the codebase; module slots are small ints.
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= 63:
        return None
    active = msg.get("active")
    # active selects inactive vs active framing: a non-bool value is unroutable.
    if not isinstance(active, bool):
        return None
    out = {"v": 1, "type": "tx_meter", "slot": slot, "active": active}
    if not active:
        return out
    gr = _num(msg.get("gain_reduction_db"), 0.0, 60.0)
    dsp = msg.get("dsp")
    out.update(
        {
            "peak_dbfs": _num(msg.get("peak_dbfs"), -120.0, 0.0),
            "rms_dbfs": _num(msg.get("rms_dbfs"), -120.0, 0.0),
            "gain_reduction_db": 0.0 if gr is None else gr,
            "limiting": msg.get("limiting") is True,
            "dsp": dsp if isinstance(dsp, str) and dsp in _DSP_MODES else "off",
            "ceiling_dbfs": _num(msg.get("ceiling_dbfs"), -120.0, 0.0),
        }
    )
    return out
