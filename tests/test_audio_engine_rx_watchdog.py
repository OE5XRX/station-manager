# tests/test_audio_engine_rx_watchdog.py
"""AudioEngine RX: real RTP-ts passthrough (§5.3 ts) + the RX health watchdog."""

import asyncio
import logging

from station_agent.audio import frame
from station_agent.audio.engine import AudioEngine


class FakeBackend:
    def list_audio_slots(self):
        return [1]

    def resolve_node(self, slot, direction):
        return f"node{slot}"


class FakeHealth:
    def __init__(self):
        self.reason = None
        self.packets = 0

    def verdict(self):
        return self.reason


class FakeRx:
    def __init__(self, node, rate, on_opus):
        self.node, self.rate, self.on_opus = node, rate, on_opus
        self.started = self.stopped = False
        self.health = FakeHealth()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeFactory:
    def __init__(self):
        self.rx = []

    def make_rx(self, node, rate, on_opus):
        b = FakeRx(node, rate, on_opus)
        self.rx.append(b)
        return b


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_engine(**kw):
    factory = FakeFactory()
    sent_json, sent_bin = [], []

    async def emit_json(msg):
        sent_json.append(msg)

    eng = AudioEngine(
        FakeBackend(),
        bridge_factory=factory,
        emit_json=emit_json,
        emit_binary=sent_bin.append,
        **kw,
    )
    return eng, factory, sent_json, sent_bin


def _subscribed(eng):
    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")

    asyncio.run(go())


# --- §5.3 ts from the real RTP timestamp ------------------------------------------------


def test_ts_follows_rtp_timestamp_at_stream_rate():
    eng, factory, _, sent_bin = make_engine()
    _subscribed(eng)
    rx = factory.rx[0]
    base = 123456
    # 960 = one 20 ms frame; 1984 = a frame + a lost graph cycle (the start-race signature)
    for rtp_ts in (base, base + 960, base + 1920, base + 3904, base + 4864):
        rx.on_opus(b"\xfc" * 40, rtp_ts)
    ts = [frame.parse_frame(d).ts for d in sent_bin]
    # 48 kHz RTP clock → 8 kHz stream samples, first packet at 0; the lost cycle stays a gap
    assert ts == [0, 160, 320, 650, 810]
    assert [frame.parse_frame(d).seq for d in sent_bin] == [0, 1, 2, 3, 4]


def test_ts_has_no_rounding_drift_with_pts_jitter():
    eng, factory, _, sent_bin = make_engine()
    _subscribed(eng)
    rx = factory.rx[0]
    rtp_ts = 0
    for i in range(300):
        rtp_ts += (956, 964)[i % 2]  # healthy PipeWire PTS jitter, 960 on average
        rx.on_opus(b"\xfc" * 40, rtp_ts)
    # 299 increments averaging 960 → 299 frames of 160 samples, no accumulated truncation
    assert abs(frame.parse_frame(sent_bin[-1]).ts - 299 * 160) <= 1


def test_ts_survives_rtp_wraparound():
    eng, factory, _, sent_bin = make_engine()
    _subscribed(eng)
    rx = factory.rx[0]
    rx.on_opus(b"\xfc" * 40, 0xFFFFFFFF - 479)
    rx.on_opus(b"\xfc" * 40, 480)  # +960 across the 2^32 wrap
    assert [frame.parse_frame(d).ts for d in sent_bin] == [0, 160]


def test_ts_duplicate_or_backwards_rtp_advances_one_frame():
    eng, factory, _, sent_bin = make_engine()
    _subscribed(eng)
    rx = factory.rx[0]
    rx.on_opus(b"\xfc" * 40, 10000)
    rx.on_opus(b"\xfc" * 40, 10000)  # duplicate
    rx.on_opus(b"\xfc" * 40, 9040)  # backwards
    rx.on_opus(b"\xfc" * 40, 11920)  # forward again: +1920 from the newest seen (10000)
    # strictly increasing — a receiver never sees ts go backwards or repeat
    assert [frame.parse_frame(d).ts for d in sent_bin] == [0, 160, 320, 640]


def test_ts_without_rtp_timestamp_counts_20ms_frames():
    """A bridge that does not pass an RTP ts keeps the old pacing (one frame per packet)."""
    eng, factory, _, sent_bin = make_engine()
    _subscribed(eng)
    rx = factory.rx[0]
    rx.on_opus(b"\xfc" * 40)
    rx.on_opus(b"\xfc" * 40)
    assert [frame.parse_frame(d).ts for d in sent_bin] == [0, 160]


# --- watchdog ------------------------------------------------------------------------


def _run_ticks(eng, clock, steps):
    """Drive the watchdog: each step is (advance_seconds, callable_before_tick | None)."""

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        for advance, before in steps:
            clock.t += advance
            if before is not None:
                before()
            await eng.rx_watchdog_tick()
        await eng.stop()

    asyncio.run(go())


def test_watchdog_restarts_unhealthy_bridge_and_logs(caplog):
    clock = Clock()
    eng, factory, sent_json, _ = make_engine(clock=clock)

    def break_it():
        factory.rx[0].health.reason = "lost frames: 30 of 200 signal intervals skip >1 frame"

    with caplog.at_level(logging.WARNING, logger="station_agent.audio.engine"):
        _run_ticks(eng, clock, [(2, break_it)])
    assert len(factory.rx) == 2
    assert factory.rx[0].stopped and factory.rx[1].started
    assert any("watchdog" in r.message and "lost frames" in r.message for r in caplog.records)
    lives = [m for m in sent_json if m["type"] == "stream_state" and m["state"] == "live"]
    assert len(lives) == 2


def test_watchdog_leaves_healthy_bridge_alone():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock)
    _run_ticks(eng, clock, [(2, None)] * 5)
    assert len(factory.rx) == 1


def test_watchdog_restarts_are_rate_limited_with_backoff():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_restart_backoff=(60.0, 900.0))

    def stall_latest():
        factory.rx[-1].health.reason = "no RTP packet for 12.0 s"

    steps = [(2, stall_latest)]  # t=2: restart #1, next allowed +60
    steps += [(10, stall_latest)] * 5  # t=52: still within 60 s → no restart
    steps += [(10, stall_latest)]  # t=62: restart #2, backoff doubles → +120
    steps += [(60, stall_latest)]  # t=122: within 120 s → none
    steps += [(61, stall_latest)]  # t=183: restart #3
    _run_ticks(eng, clock, steps)
    assert len(factory.rx) == 4


def test_watchdog_backoff_resets_once_a_bridge_delivers():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_restart_backoff=(60.0, 900.0))

    def stall_latest():
        factory.rx[-1].health.reason = "no RTP packet for 12.0 s"

    def heal_latest():
        h = factory.rx[-1].health
        h.reason, h.packets = None, 50

    # t=2 restart #1 (+60), t=63 restart #2 (backoff doubled → +120), t=64 the new bridge
    # delivers → streak reset, t=124 = 61 s after #2: without the reset this would wait for
    # t=183, with it the base 60 s interval applies again.
    steps = [(2, stall_latest), (61, stall_latest), (1, heal_latest), (60, stall_latest)]
    _run_ticks(eng, clock, steps)
    assert len(factory.rx) == 4


def test_watchdog_ignores_bridges_without_health():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        del factory.rx[0].health
        clock.t += 5
        await eng.rx_watchdog_tick()
        await eng.stop()

    asyncio.run(go())
    assert len(factory.rx) == 1


def test_watchdog_task_runs_in_background_and_stops_with_engine():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_watchdog_interval=0.01)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        factory.rx[0].health.reason = "no RTP packet for 12.0 s"
        for _ in range(100):
            if len(factory.rx) > 1:
                break
            await asyncio.sleep(0.01)
        await eng.stop()
        assert eng._rx_watchdog is None

    asyncio.run(go())
    assert len(factory.rx) == 2


def test_watchdog_skips_unsubscribed_stream():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        factory.rx[0].health.reason = "no RTP packet for 12.0 s"
        await eng.on_source_unsubscribe("slot1.rx")
        clock.t += 5
        await eng.rx_watchdog_tick()
        await eng.stop()

    asyncio.run(go())
    assert len(factory.rx) == 1
