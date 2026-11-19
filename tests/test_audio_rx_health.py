# tests/test_audio_rx_health.py
"""RX health tracker: stall detection + lost-frame (RTP-ts gap) detection outside DTX."""

from station_agent.audio.rx_health import RxHealth

SPEECH = 40  # payload bytes of a normal Opus voice packet
DTX = 1  # ≤2 bytes = Opus DTX comfort frame


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _feed(h, clock, incs, size=SPEECH, ts=0):
    for inc in incs:
        ts = (ts + inc) & 0xFFFFFFFF
        clock.t += 0.02
        h.record(ts, size)
    return ts


def test_healthy_stream_has_no_verdict():
    clock = Clock()
    h = RxHealth(clock=clock)
    h.record(0, SPEECH)
    _feed(h, clock, [960, 956, 964] * 200)
    clock.t += 0.1
    assert h.verdict() is None


def test_stream_losing_every_fourth_frame_is_gappy():
    """The pipewiresrc start race: one graph cycle lost every 4 packets (37.5/s, 960/1984)."""
    clock = Clock()
    h = RxHealth(clock=clock)
    h.record(0, SPEECH)
    _feed(h, clock, [960, 960, 960, 1984] * 150)
    clock.t += 0.1
    reason = h.verdict()
    assert reason is not None and "lost frames" in reason


def test_gaps_around_dtx_are_legitimate():
    """DTX phases show big ts jumps with tiny packets — never counted as loss."""
    clock = Clock()
    h = RxHealth(clock=clock)
    ts = 0
    h.record(ts, SPEECH)
    for _ in range(60):
        ts = _feed(h, clock, [960] * 5, ts=ts)  # speech
        ts = _feed(h, clock, [960, 7680], size=DTX, ts=ts)  # DTX, sparse
        ts = _feed(h, clock, [7680], ts=ts)  # speech resumes after a DTX gap
    clock.t += 10
    h.record(ts + 960, SPEECH)
    assert h.verdict() is None


def test_too_few_signal_frames_gives_no_gap_verdict():
    clock = Clock()
    h = RxHealth(clock=clock)
    h.record(0, SPEECH)
    _feed(h, clock, [1984] * 20)  # 20 bad pairs, but far below the evidence minimum
    clock.t += 10
    h.record(99999, SPEECH)
    assert h.verdict() is None


def test_gap_window_resets_after_evaluation():
    clock = Clock()
    h = RxHealth(clock=clock, window=10.0)
    h.record(0, SPEECH)
    ts = _feed(h, clock, [960] * 600)  # 12 s healthy → first window evaluated clean
    assert h.verdict() is None
    _feed(h, clock, [960, 960, 960, 1984] * 150, ts=ts)
    clock.t += 0.1
    assert h.verdict() is not None


def test_no_packet_since_start_is_stalled():
    clock = Clock()
    h = RxHealth(clock=clock, stall_timeout=10.0)
    clock.t += 9.9
    assert h.verdict() is None
    clock.t += 0.2
    reason = h.verdict()
    assert reason is not None and "no RTP packet" in reason


def test_stall_after_packets_measured_from_last_packet():
    clock = Clock()
    h = RxHealth(clock=clock, stall_timeout=10.0)
    clock.t += 30  # slow start (preroll) does not count once packets flow
    h.record(0, SPEECH)
    clock.t += 9
    assert h.verdict() is None
    clock.t += 2
    assert "no RTP packet" in h.verdict()


def test_duplicate_or_backwards_ts_is_not_a_gap():
    clock = Clock()
    h = RxHealth(clock=clock)
    h.record(5000, SPEECH)
    ts = 5000
    for _ in range(300):
        clock.t += 0.02
        h.record(ts, SPEECH)  # duplicate
        clock.t += 0.02
        h.record((ts - 960) & 0xFFFFFFFF, SPEECH)  # backwards
        ts += 960
        h.record(ts, SPEECH)
    clock.t += 0.1
    assert h.verdict() is None


def test_ts_wraparound_is_not_a_gap():
    clock = Clock()
    h = RxHealth(clock=clock)
    start = 0xFFFFFFFF - 960 * 100
    h.record(start, SPEECH)
    _feed(h, clock, [960] * 600, ts=start)
    clock.t += 0.1
    assert h.verdict() is None


def test_packets_counter():
    clock = Clock()
    h = RxHealth(clock=clock)
    assert h.packets == 0
    h.record(0, SPEECH)
    h.record(960, DTX)
    assert h.packets == 2


def test_lost_frames_verdict_is_sticky_through_silent_windows():
    """Between evaluations and through windows without enough signal, a known-lossy stream
    stays lossy — otherwise the watchdog would read "healthy" and forget its backoff."""
    clock = Clock()
    h = RxHealth(clock=clock, window=10.0)
    h.record(0, SPEECH)
    ts = _feed(h, clock, [960, 960, 960, 1984] * 150)
    clock.t += 0.1
    assert h.verdict() is not None
    clock.t += 2
    assert h.verdict() is not None  # mid-window: still known bad
    ts = _feed(h, clock, [960, 7680] * 300, size=DTX, ts=ts)  # 12 s of DTX only
    assert h.verdict() is not None  # silent window: no evidence either way
    _feed(h, clock, [960] * 600, ts=ts)  # 12 s of clean signal
    assert h.verdict() is None
