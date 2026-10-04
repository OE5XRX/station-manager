"""Thread-safe holder for server-pushed TX-audio calibration (spec §4).

Written by the heartbeat loop (main thread), read by the audio thread at each TX-bridge
start. The agent re-clamps every value — it never trusts the wire for an RF-safety bound.
"""

from __future__ import annotations

import logging
import math
import threading

logger = logging.getLogger(__name__)

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def clamp_ceiling(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CEILING_DEFAULT_DBFS
    try:
        f = float(value)
    except (OverflowError, ValueError, TypeError):
        return CEILING_DEFAULT_DBFS
    if not math.isfinite(f):
        return CEILING_DEFAULT_DBFS
    return min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, f))


class TxAudioSettings:
    def __init__(self):
        self._lock = threading.Lock()
        self._ceiling = CEILING_DEFAULT_DBFS

    @property
    def ceiling_dbfs(self) -> float:
        with self._lock:
            return self._ceiling

    def update_from_heartbeat(self, body) -> None:
        """Total by contract: never raises; any failure resets to the safe default."""
        try:
            raw = None
            if isinstance(body, dict):
                tx = body.get("tx_audio")
                if isinstance(tx, dict):
                    raw = tx.get("ceiling_dbfs")
            value = clamp_ceiling(raw)
        except Exception:  # noqa: BLE001 - hostile wire data must never escape
            logger.warning("tx_audio heartbeat body unusable; using default ceiling")
            value = CEILING_DEFAULT_DBFS
        with self._lock:
            self._ceiling = value
