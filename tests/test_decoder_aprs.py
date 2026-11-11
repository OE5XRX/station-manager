import asyncio
import base64

import pytest

from station_agent.decoders.aprs import AprsDecoder


def test_env_contents():
    assert AprsDecoder().env(2, {}, "alsa_input.x") == {
        "OE5XRX_DECODER": "aprs",
        "OE5XRX_SLOT": "2",
        "OE5XRX_RX_NODE": "alsa_input.x",
        "OE5XRX_KISS_PORT": "8102",
        "OE5XRX_SAMPLE_RATE": "48000",
    }


class _Writer:
    def close(self):
        pass


def test_run_emits_ax25_frames_with_heard_at():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xc0\x00ab\xc0\xc0\x00c\xdb\xdc\xc0")
        reader.feed_eof()
        calls = []
        emitted = []

        async def connect(host, port):
            calls.append((host, port))
            if len(calls) > 1:
                raise asyncio.CancelledError  # stop after the EOF-triggered reconnect
            return reader, _Writer()

        async def emit(kind, reliable, data):
            emitted.append((kind, reliable, data))

        with pytest.raises(asyncio.CancelledError):
            await AprsDecoder().run(1, emit, connect=connect, now=lambda: 100.0)
        return calls, emitted

    calls, emitted = asyncio.run(scenario())
    assert calls == [("127.0.0.1", 8101), ("127.0.0.1", 8101)]
    assert emitted == [
        ("ax25", True, {"ax25": base64.b64encode(b"ab").decode(), "heard_at": 100.0}),
        ("ax25", True, {"ax25": base64.b64encode(b"c\xc0").decode(), "heard_at": 100.0}),
    ]


def test_run_retries_then_connects():
    async def scenario():
        t = [0.0]
        attempts = []

        async def sleep(s):
            t[0] += s

        async def connect(host, port):
            attempts.append(t[0])
            if len(attempts) < 3:
                raise ConnectionRefusedError
            raise asyncio.CancelledError  # connected on the 3rd try; stop here

        async def emit(*a):
            pass

        with pytest.raises(asyncio.CancelledError):
            await AprsDecoder().run(
                1, emit, connect=connect, now=lambda: t[0], monotonic=lambda: t[0], sleep=sleep
            )
        return attempts

    assert asyncio.run(scenario())[:3] == [0.0, 0.5, 1.0]


def test_run_gives_up_after_20s_without_listener():
    async def scenario():
        t = [0.0]

        async def sleep(s):
            t[0] += s

        async def connect(host, port):
            raise ConnectionRefusedError

        async def emit(*a):
            pass

        await AprsDecoder().run(
            1, emit, connect=connect, now=lambda: t[0], monotonic=lambda: t[0], sleep=sleep
        )

    with pytest.raises(ConnectionError):
        asyncio.run(scenario())


def test_run_backs_off_after_eof_and_gives_up_on_silent_listener():
    async def scenario():
        sleeps = []

        async def sleep(s):
            sleeps.append(s)

        async def connect(host, port):
            r = asyncio.StreamReader()
            r.feed_eof()
            return r, _Writer()

        async def emit(*a):
            pass

        with pytest.raises(ConnectionError):
            await AprsDecoder().run(1, emit, connect=connect, sleep=sleep)
        return sleeps

    sleeps = asyncio.run(scenario())
    assert len(sleeps) == 4 and all(s > 0 for s in sleeps)


def test_run_reconnects_on_connection_reset():
    async def scenario():
        calls = []

        class Reset:
            async def read(self, n):
                raise ConnectionResetError

        async def connect(host, port):
            calls.append(1)
            if len(calls) > 1:
                raise asyncio.CancelledError
            return Reset(), _Writer()

        async def emit(*a):
            pass

        async def sleep(s):
            pass

        with pytest.raises(asyncio.CancelledError):
            await AprsDecoder().run(1, emit, connect=connect, sleep=sleep)
        return len(calls)

    assert asyncio.run(scenario()) == 2


# --- aprs.rx_packets_1h metric (spec 2026-11-03 generic metrics) -------------------------


def _drive(dec, slot, frames_at, *, t):
    """Run the plugin against a reader pre-fed with ``frames_at`` KISS frames."""

    async def scenario():
        reader = asyncio.StreamReader()
        for _ in range(frames_at):
            reader.feed_data(b"\xc0\x00ab\xc0")
        calls = []

        async def connect(host, port):
            calls.append(1)
            if len(calls) > 1:
                raise asyncio.CancelledError
            reader.feed_eof()
            return reader, _Writer()

        async def emit(kind, reliable, data):
            pass

        with pytest.raises(asyncio.CancelledError):
            await dec.run(slot, emit, connect=connect, monotonic=lambda: t[0])

    asyncio.run(scenario())


def test_metric_absent_without_running_decoder():
    assert AprsDecoder().metric_values(now=1000.0) == {}


def test_metric_absent_until_decoder_ran_60_min():
    dec = AprsDecoder()
    dec._track_start(1, 0.0)
    dec._track_packet(1, 10.0)
    assert dec.metric_values(now=3599.0) == {}
    assert dec.metric_values(now=3600.0) == {"aprs.rx_packets_1h": 1.0}


def test_metric_counts_sliding_window_and_zero_is_reported():
    dec = AprsDecoder()
    dec._track_start(1, 0.0)
    for ts in (100.0, 200.0, 3000.0):
        dec._track_packet(1, ts)
    assert dec.metric_values(now=3700.0) == {"aprs.rx_packets_1h": 2.0}  # 100 aged out
    assert dec.metric_values(now=9000.0) == {"aprs.rx_packets_1h": 0.0}


def test_metric_sums_slots_but_needs_all_slots_old_enough():
    dec = AprsDecoder()
    dec._track_start(1, 0.0)
    dec._track_start(2, 0.0)
    dec._track_packet(1, 5.0)
    dec._track_packet(2, 6.0)
    assert dec.metric_values(now=3600.0) == {"aprs.rx_packets_1h": 2.0}
    dec._track_start(3, 3000.0)  # young decoder: sum would be misleading
    assert dec.metric_values(now=3600.0) == {}
    dec._track_stop(3)
    assert dec.metric_values(now=3600.0) == {"aprs.rx_packets_1h": 2.0}


def test_run_counts_frames_and_stops_tracking_on_exit():
    dec = AprsDecoder()
    t = [0.0]
    _drive(dec, 1, 3, t=t)
    # run() ended (cancelled): tracking dropped, so the key is gone again
    assert dec.metric_values(now=10_000.0) == {}


def test_run_records_each_frame():
    dec = AprsDecoder()
    t = [0.0]
    seen = []
    orig = dec._track_packet
    dec._track_packet = lambda slot, ts: (seen.append((slot, ts)), orig(slot, ts))
    _drive(dec, 4, 3, t=t)
    assert seen == [(4, 0.0)] * 3


def test_metric_descriptor_and_collector_source():
    from station_agent.metrics import MetricsCollector, describe

    d = describe("aprs.rx_packets_1h")
    assert d["unit"] == "pkts"
    assert d["min"] == 0 and d["precision"] == 0

    collector = MetricsCollector(root="/nonexistent")
    collector.add_source(lambda: {"aprs.rx_packets_1h": 4.0})
    values, descriptors = collector.snapshot()
    assert values["aprs.rx_packets_1h"] == 4.0
    assert any(x["key"] == "aprs.rx_packets_1h" for x in descriptors)

    collector2 = MetricsCollector(root="/nonexistent")
    collector2.add_source(lambda: {})
    collector2.add_source(lambda: 1 / 0)  # failing source only omits its keys
    values2, _ = collector2.snapshot()
    assert "aprs.rx_packets_1h" not in values2
