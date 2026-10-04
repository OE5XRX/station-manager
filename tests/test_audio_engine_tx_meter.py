import asyncio

from tests.test_audio_engine import make_engine

NODES = {(1, "tx"): "oe5xrx.slot1"}
READING = {
    "peak_dbfs": -12.0,
    "rms_dbfs": -18.0,
    "gain_reduction_db": 3.1,
    "limiting": True,
    "dsp": "full",
    "ceiling_dbfs": -12.0,
}


def _meters(sent_json):
    return [m for m in sent_json if m.get("type") == "tx_meter"]


def test_meter_reading_from_bridge_thread_is_emitted_as_tx_meter():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        on_meter = factory.last_tx_on_meter
        assert on_meter is not None
        # called from a foreign thread, like the real reader
        await asyncio.get_running_loop().run_in_executor(None, on_meter, READING)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)

    asyncio.run(scenario())
    active = [m for m in _meters(sent_json) if m["active"]]
    assert active[-1] == {"v": 1, "type": "tx_meter", "slot": 1, "active": True, **READING}


def test_teardown_emits_inactive_meter_and_late_readings_dropped():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        on_meter = factory.last_tx_on_meter
        await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)
        assert {"v": 1, "type": "tx_meter", "slot": 1, "active": False} in sent_json
        n = len(sent_json)
        on_meter(READING)  # a late reading from a torn-down bridge
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sent_json) == n

    asyncio.run(scenario())


def test_initial_frame_surfaces_dsp_mode_even_without_reader():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        await asyncio.sleep(0)  # let the initial frame drain before teardown
        await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)

    # Fake bridge has no dsp_mode/ceiling_dbfs -> safe defaults.
    asyncio.run(scenario())
    first = _meters(sent_json)[0]
    assert first == {
        "v": 1,
        "type": "tx_meter",
        "slot": 1,
        "active": True,
        "peak_dbfs": None,
        "rms_dbfs": None,
        "gain_reduction_db": 0.0,
        "limiting": False,
        "dsp": "off",
        "ceiling_dbfs": None,
    }


def test_initial_frame_reports_bridge_dsp_mode_and_ceiling():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)
    orig = factory.make_tx

    def make_tx(node, rate, on_meter=None):
        b = orig(node, rate, on_meter=on_meter)
        b.dsp_mode = "failed"
        b.ceiling_dbfs = -14.0
        return b

    factory.make_tx = make_tx

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        await asyncio.sleep(0)  # let the initial frame drain before teardown
        await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)

    asyncio.run(scenario())
    first = _meters(sent_json)[0]
    assert first["dsp"] == "failed" and first["ceiling_dbfs"] == -14.0


def test_failed_start_emits_no_meter_and_drops_readings():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)
    orig = factory.make_tx

    def make_tx(node, rate, on_meter=None):
        b = orig(node, rate, on_meter=on_meter)

        def boom():
            raise RuntimeError("no gst")

        b.start = boom
        return b

    factory.make_tx = make_tx

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        factory.last_tx_on_meter(READING)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert _meters(sent_json) == []


def _reading(peak):
    return {**READING, "peak_dbfs": peak}


def test_meter_emit_is_latest_wins_with_one_in_flight():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)

    async def scenario():
        await eng.start()
        gate = asyncio.Event()
        sent = []
        orig = eng._emit_json

        async def slow_emit(msg):
            if msg.get("type") == "tx_meter" and msg["active"]:
                sent.append(msg)
                await gate.wait()
            else:
                await orig(msg)

        eng._emit_json = slow_emit
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        await asyncio.sleep(0)  # initial frame send now blocked in-flight
        on_meter = factory.last_tx_on_meter
        for peak in (-30.0, -20.0, -10.0):
            on_meter(_reading(peak))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(sent) == 1  # one in flight, the rest coalesced
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        return sent

    sent = asyncio.run(scenario())
    assert [m["peak_dbfs"] for m in sent] == [None, -10.0]


def test_superseded_bridge_readings_dropped_same_and_different_slot():
    for second_slot in (1, 3):
        nodes = {(1, "tx"): "n1", (3, "tx"): "n3"}
        eng, factory, sent_json, _ = make_engine(slots=(1, 3), nodes=nodes)

        async def scenario():
            await eng.start()
            await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
            on_meter1 = factory.last_tx_on_meter
            await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)
            await eng.on_mic_state(active=True, tx_slot=second_slot, tx_module="fm")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            n = len(_meters(sent_json))
            on_meter1(READING)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(_meters(sent_json)) == n
            await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)

        asyncio.run(scenario())


def test_on_meter_swallows_closed_loop_runtime_error():
    eng, factory, sent_json, _ = make_engine(nodes=NODES)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        return factory.last_tx_on_meter

    on_meter = asyncio.run(scenario())  # loop is now closed
    on_meter(READING)  # must not raise
