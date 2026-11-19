"""RX bridge health tracker — pure, no I/O (fed by the RX reader, judged by the engine).

The RX bridge's gst pipeline can be alive yet broken, and process liveness cannot see it:

- **stalled** — the process runs but delivers nothing: after a PipeWire restart the pinned
  stream (``node.dont-reconnect``) stays unlinked, and a non-live pipeline whose source
  never delivers sits in PREROLLING forever.
- **lost frames** — pipewiresrc's start race (CLOCK_LOST on the initial
  streaming→paused→streaming, PipeWire ≤ 1.6.11) leaves a stream that permanently drops one
  graph cycle in every 4–6: ~37.5 instead of 50 packets/s, RTP-ts increments 960/1984 mixed.

The RTP timestamp (48 kHz Opus clock, 960 ticks per 20 ms frame) is the reliable signal:
between two consecutive *signal* packets it advances by one frame, any more means audio was
lost upstream of the encoder. Gaps that touch a DTX packet (≤2 bytes) are legitimate — the
encoder goes sparse in silence — and are never counted.
"""

from __future__ import annotations

import threading
import time

RTP_TS_PER_FRAME = 960  # Opus RTP clock (RFC 7587), 20 ms frame
_DTX_MAX_BYTES = 2
# An increment above 1.5 frames means at least one frame of audio is missing (PipeWire PTS
# jitter makes healthy increments 956/960/964).
_GAP_TICKS = RTP_TS_PER_FRAME * 3 // 2
_HALF_RANGE = 1 << 31
_U32 = 0xFFFFFFFF


class RxHealth:
    """Thread-safe: ``record`` runs on the bridge reader thread, ``verdict`` on the loop.

    ``verdict`` evaluates the lost-frame ratio once per ``window`` seconds (then starts a new
    window) and only with at least ``min_pairs`` signal→signal intervals of evidence, so a
    stream that is mostly silent (DTX) is never judged on a handful of frames. The result is
    sticky until the next window with enough evidence: a lossy stream stays "lost frames"
    between evaluations and through silent windows, so None really means "not known bad".
    """

    def __init__(
        self,
        *,
        clock=time.monotonic,
        stall_timeout: float = 10.0,
        window: float = 10.0,
        min_pairs: int = 100,
        max_gap_ratio: float = 0.05,
    ):
        self._clock = clock
        self._stall_timeout = stall_timeout
        self._window = window
        self._min_pairs = min_pairs
        self._max_gap_ratio = max_gap_ratio
        self._lock = threading.Lock()
        now = clock()
        self._last_packet_at = now  # start counts as "last packet" for the stall timeout
        self._window_start = now
        self._max_ts: int | None = None  # highest RTP ts seen (wrap-aware)
        self._prev_dtx = True  # no signal pair before the first signal packet
        self._pairs = 0
        self._gaps = 0
        self._gap_reason: str | None = None
        self.packets = 0

    def record(self, rtp_ts: int, payload_len: int) -> None:
        dtx = payload_len <= _DTX_MAX_BYTES
        with self._lock:
            self.packets += 1
            self._last_packet_at = self._clock()
            if self._max_ts is None:
                self._max_ts = rtp_ts
                self._prev_dtx = dtx
                return
            inc = (rtp_ts - self._max_ts) & _U32
            forward = 0 < inc < _HALF_RANGE
            if forward and not dtx and not self._prev_dtx:
                self._pairs += 1
                if inc > _GAP_TICKS:
                    self._gaps += 1
            if forward:
                self._max_ts = rtp_ts
            self._prev_dtx = dtx

    def verdict(self) -> str | None:
        """None while healthy, else a human-readable reason to restart the bridge."""
        with self._lock:
            now = self._clock()
            silent = now - self._last_packet_at
            if silent > self._stall_timeout:
                return f"no RTP packet for {silent:.1f} s"
            if now - self._window_start >= self._window:
                pairs, gaps = self._pairs, self._gaps
                self._pairs = self._gaps = 0
                self._window_start = now
                if pairs >= self._min_pairs:
                    self._gap_reason = (
                        f"lost frames: {gaps} of {pairs} signal intervals skip >1 frame"
                        if gaps > pairs * self._max_gap_ratio
                        else None
                    )
            return self._gap_reason
