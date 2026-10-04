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
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    if "active" not in msg:
        return None
    out = {"v": 1, "type": "tx_meter", "slot": slot, "active": bool(msg.get("active"))}
    if not out["active"]:
        return out
    gr = _num(msg.get("gain_reduction_db"), 0.0, 60.0)
    dsp = msg.get("dsp")
    out.update(
        {
            "peak_dbfs": _num(msg.get("peak_dbfs"), -120.0, 0.0),
            "rms_dbfs": _num(msg.get("rms_dbfs"), -120.0, 0.0),
            "gain_reduction_db": 0.0 if gr is None else gr,
            "limiting": bool(msg.get("limiting")),
            "dsp": dsp if isinstance(dsp, str) and dsp in _DSP_MODES else "off",
            "ceiling_dbfs": _num(msg.get("ceiling_dbfs"), -120.0, 0.0),
        }
    )
    return out
