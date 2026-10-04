"""Thread-safe holder for server-pushed TX-audio calibration (spec §4).

Written by the heartbeat loop (main thread), read by the audio thread at each TX-bridge
start. The agent re-clamps every value — it never trusts the wire for an RF-safety bound.
"""

from __future__ import annotations

import math
import threading

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def clamp_ceiling(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CEILING_DEFAULT_DBFS
    if not math.isfinite(value):
        return CEILING_DEFAULT_DBFS
    return float(min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, value)))


class TxAudioSettings:
    def __init__(self):
        self._lock = threading.Lock()
        self._ceiling = CEILING_DEFAULT_DBFS

    @property
    def ceiling_dbfs(self) -> float:
        with self._lock:
            return self._ceiling

    def update_from_heartbeat(self, body) -> None:
        raw = None
        if isinstance(body, dict):
            tx = body.get("tx_audio")
            if isinstance(tx, dict):
                raw = tx.get("ceiling_dbfs")
        value = clamp_ceiling(raw)
        with self._lock:
            self._ceiling = value
