# tests/test_audio_engine_rx_watchdog.py
"""AudioEngine RX: real RTP-ts passthrough (§5.3 ts) + the RX health watchdog."""

import asyncio
import logging

from station_agent.audio import frame
from station_agent.audio.engine import AudioEngine


class FakeBackend:
    def __init__(self):
        self.node_gone = False

    def list_audio_slots(self):
        return [1]

    def resolve_node(self, slot, direction):
        return None if self.node_gone else f"node{slot}"


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


def test_watchdog_keeps_doubling_for_a_delivering_but_lossy_bridge():
    """Packets flowing is not "healthy": a bridge with a (sticky) lost-frames verdict keeps
    the streak, so a non-race loss cause is not restarted every base interval forever."""
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_restart_backoff=(60.0, 900.0))

    def lossy_latest():
        h = factory.rx[-1].health
        h.reason, h.packets = "lost frames: 40 of 200 signal intervals skip >1 frame", 500

    # restarts at t=2, 62 (+120 next), 182 (+240 next); t=300 is still inside 240 s
    _run_ticks(
        eng,
        clock,
        [(2, lossy_latest), (60, lossy_latest), (120, lossy_latest)] + [(118, lossy_latest)],
    )
    assert len(factory.rx) == 4


def test_watchdog_backoff_resets_after_a_healthy_interval():
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_restart_backoff=(60.0, 900.0))

    def stall_latest():
        factory.rx[-1].health.reason = "no RTP packet for 12.0 s"

    def heal_latest():
        factory.rx[-1].health.reason = None

    # t=2 restart #1, t=62 restart #2 (next +120). The new bridge is healthy through t=182
    # (a whole interval) → streak forgotten, so a fault at t=183 restarts at once (#3),
    # and the one after that is gated by the base interval again (t=240 < 183+60).
    steps = [(2, stall_latest), (60, stall_latest), (1, heal_latest), (119, heal_latest)]
    steps += [(1, stall_latest), (57, stall_latest)]
    _run_ticks(eng, clock, steps)
    assert len(factory.rx) == 4


def test_watchdog_restart_continues_seq_and_ts():
    """The browser keeps its jitter buffer per stream_ref: a restarted bridge must continue
    seq/ts, or its frames are dropped as too late until seq catches up again."""
    clock = Clock()
    eng, factory, _, sent_bin = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        old = factory.rx[0]
        for i in range(3):
            old.on_opus(b"\xfc" * 40, 5000 + 960 * i)
        old.health.reason = "lost frames: 40 of 200 signal intervals skip >1 frame"
        clock.t += 5
        await eng.rx_watchdog_tick()
        new = factory.rx[1]
        new.on_opus(b"\xfc" * 40, 777)  # fresh gst: unrelated RTP ts base
        new.on_opus(b"\xfc" * 40, 777 + 960)
        await eng.stop()

    asyncio.run(go())
    frames = [frame.parse_frame(d) for d in sent_bin]
    assert [f.seq for f in frames] == [0, 1, 2, 3, 4]
    assert [f.ts for f in frames] == [0, 160, 320, 480, 640]
    assert [f.marker for f in frames] == [True, False, False, True, False]


def test_watchdog_restart_before_any_packet_keeps_ts_base():
    clock = Clock()
    eng, factory, _, sent_bin = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        factory.rx[0].on_opus(b"\xfc" * 40)  # ts 0, seq 0
        for _ in range(2):  # two restarts in a row, the middle bridge never delivers
            factory.rx[-1].health.reason = "no RTP packet for 12.0 s"
            clock.t += 100
            await eng.rx_watchdog_tick()
        factory.rx[-1].on_opus(b"\xfc" * 40, 42)
        await eng.stop()

    asyncio.run(go())
    frames = [frame.parse_frame(d) for d in sent_bin]
    assert [(f.seq, f.ts) for f in frames] == [(0, 0), (1, 160)]


def test_stop_during_watchdog_restart_leaks_no_bridge():
    """stop() while the watchdog's bridge.start() runs in the executor: the restart finishes
    under the lock and stop() then stops it — no gst outlives the engine."""
    import threading

    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_watchdog_interval=0.01)
    release = threading.Event()
    entered = threading.Event()

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        orig_make = factory.make_rx

        def slow_make(node, rate, on_opus):
            b = orig_make(node, rate, on_opus)

            def slow_start():
                entered.set()
                release.wait(5)
                # A real RxBridge assigns its gst process only now: a stop() that already
                # ran saw no process and the one spawned here would be orphaned.
                b.leaked = b.stopped
                b.started = True

            b.start = slow_start
            return b

        factory.make_rx = slow_make
        factory.rx[0].health.reason = "no RTP packet for 12.0 s"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, entered.wait, 5)
        stopper = asyncio.ensure_future(eng.stop())
        await asyncio.sleep(0.05)
        release.set()
        await stopper

    asyncio.run(go())
    assert len(factory.rx) == 2
    assert not getattr(factory.rx[1], "leaked", False)
    assert all(b.stopped for b in factory.rx if b.started)
    assert eng._rx == {}


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


def test_failed_watchdog_restart_keeps_seq_for_the_refresh_retry():
    """The watchdog restart fails (node briefly unresolvable); the later refresh retry must
    still continue seq/ts instead of restarting at 0."""
    clock = Clock()
    eng, factory, _, sent_bin = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        for i in range(3):
            factory.rx[0].on_opus(b"\xfc" * 40, 960 * i)
        factory.rx[0].health.reason = "no RTP packet for 12.0 s"
        eng._backend.node_gone = True
        clock.t += 5
        await eng.rx_watchdog_tick()
        assert "slot1.rx" not in eng._rx
        eng._backend.node_gone = False
        await eng.refresh([1])  # retries the demanded-but-not-running stream
        factory.rx[-1].on_opus(b"\xfc" * 40, 31337)
        await eng.stop()

    asyncio.run(go())
    assert len(factory.rx) == 2
    frames = [frame.parse_frame(d) for d in sent_bin]
    assert [(f.seq, f.ts) for f in frames] == [(0, 0), (1, 160), (2, 320), (3, 480)]


def test_resume_state_dropped_on_unsubscribe():
    clock = Clock()
    eng, factory, _, sent_bin = make_engine(clock=clock)

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        factory.rx[0].on_opus(b"\xfc" * 40, 0)
        factory.rx[0].health.reason = "no RTP packet for 12.0 s"
        eng._backend.node_gone = True
        clock.t += 5
        await eng.rx_watchdog_tick()
        await eng.on_source_unsubscribe("slot1.rx")
        eng._backend.node_gone = False
        await eng.on_source_subscribe("slot1.rx")  # a fresh subscription starts at 0
        factory.rx[-1].on_opus(b"\xfc" * 40, 5)
        await eng.stop()

    asyncio.run(go())
    assert [frame.parse_frame(d).seq for d in sent_bin] == [0, 0]


def test_refresh_rebuild_continues_seq_and_ts():
    """A hot-plug rebuild of the same slot (unchanged ref) continues seq/ts like the
    watchdog restart does."""
    eng, factory, _, sent_bin = make_engine()

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        factory.rx[0].on_opus(b"\xfc" * 40, 0)
        factory.rx[0].on_opus(b"\xfc" * 40, 960)
        await eng.refresh([1], rebuild={1})
        factory.rx[-1].on_opus(b"\xfc" * 40, 99)
        await eng.stop()

    asyncio.run(go())
    assert len(factory.rx) == 2
    frames = [frame.parse_frame(d) for d in sent_bin]
    assert [(f.seq, f.ts) for f in frames] == [(0, 0), (1, 160), (2, 320)]


def test_refresh_rebuild_forgets_the_watchdog_backoff():
    """A high backoff streak (e.g. from a PipeWire outage) must not delay the watchdog for a
    bridge that a hot-plug rebuild replaced."""
    clock = Clock()
    eng, factory, _, _ = make_engine(clock=clock, rx_restart_backoff=(60.0, 900.0))

    async def go():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        for _ in range(4):  # streak 4 → next restart not before 480 s
            factory.rx[-1].health.reason = "no RTP packet for 12.0 s"
            clock.t += 1000
            await eng.rx_watchdog_tick()
        assert len(factory.rx) == 5
        await eng.refresh([1], rebuild={1})
        assert len(factory.rx) == 6
        factory.rx[-1].health.reason = "lost frames: 40 of 200 signal intervals skip >1 frame"
        clock.t += 5  # well inside 480 s, but the rebuilt stream has no streak
        await eng.rx_watchdog_tick()
        await eng.stop()

    asyncio.run(go())
    assert len(factory.rx) == 7
