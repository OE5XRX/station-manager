"""Thread-safe holder for server-pushed TX-audio calibration (spec §4).

Written by the heartbeat loop (main thread), read by the audio thread at each TX-bridge
start. The agent re-clamps every value — it never trusts the wire for an RF-safety bound — and an
unusable value never makes the ceiling louder (see ``update_from_heartbeat``).
"""

from __future__ import annotations

import logging
import math
import threading

logger = logging.getLogger(__name__)

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def _parse_ceiling(value) -> float | None:
    """Clamped ceiling for a finite real number, ``None`` for anything unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(f):
        return None
    return min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, f))


def clamp_ceiling(value) -> float:
    parsed = _parse_ceiling(value)
    return CEILING_DEFAULT_DBFS if parsed is None else parsed


class TxAudioSettings:
    def __init__(self):
        self._lock = threading.Lock()
        self._ceiling = CEILING_DEFAULT_DBFS

    @property
    def ceiling_dbfs(self) -> float:
        with self._lock:
            return self._ceiling

    def update_from_heartbeat(self, body) -> None:
        """Total by contract: never raises.

        A valid server value (finite number, clamped) is authoritative and applied as-is,
        louder or quieter. Anything unusable (non-dict body, missing ``tx_audio``, JSON
        error → ``None``, invalid ceiling) yields ``min(current, default)``: never louder
        than the current value nor than the default (spec §4: fail toward under-deviation).
        """
        try:
            value = None
            if isinstance(body, dict):
                tx = body.get("tx_audio")
                if isinstance(tx, dict):
                    value = _parse_ceiling(tx.get("ceiling_dbfs"))
        except Exception:  # noqa: BLE001 - hostile wire data must never escape
            logger.warning("tx_audio heartbeat body unusable; not raising the ceiling")
            value = None
        with self._lock:
            if value is None:
                value = min(self._ceiling, CEILING_DEFAULT_DBFS)
            self._ceiling = value
