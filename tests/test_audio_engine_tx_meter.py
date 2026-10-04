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
