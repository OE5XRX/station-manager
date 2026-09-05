# tests/test_audio_engine.py
"""AudioEngine: advertise, demand-gated RX bridges, mic→TX, dead-man, tx_route."""

import asyncio

from station_agent.audio import frame
from station_agent.audio.engine import AudioEngine


class FakeBackend:
    def __init__(self, slots, nodes=None):
        self._slots = slots
        self._nodes = nodes or {}

    def list_audio_slots(self):
        return list(self._slots)

    def resolve_node(self, slot, direction):
        return self._nodes.get((slot, direction))


class FakeRx:
    def __init__(self, node, rate, on_opus):
        self.node, self.rate, self.on_opus = node, rate, on_opus
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeTx:
    def __init__(self, node, rate):
        self.node, self.rate = node, rate
        self.started = self.stopped = False
        self.fed = []

    def start(self):
        self.started = True

    def feed_opus(self, payload):
        self.fed.append(payload)

    def stop(self):
        self.stopped = True


class FakeFactory:
    def __init__(self):
        self.rx = []
        self.tx = []

    def make_rx(self, node, rate, on_opus):
        b = FakeRx(node, rate, on_opus)
        self.rx.append(b)
        return b

    def make_tx(self, node, rate):
        b = FakeTx(node, rate)
        self.tx.append(b)
        return b


def make_engine(slots=(1,), nodes=None, dead_man=0.05):
    backend = FakeBackend(slots, nodes)
    factory = FakeFactory()
    sent_json = []
    sent_bin = []

    async def emit_json(msg):
        sent_json.append(msg)

    def emit_binary(data):
        sent_bin.append(data)

    eng = AudioEngine(
        backend,
        bridge_factory=factory,
        emit_json=emit_json,
        emit_binary=emit_binary,
        dead_man_timeout=dead_man,
    )
    return eng, factory, sent_json, sent_bin


def test_advertise_lists_slots_and_mic():
    eng, _, sent_json, _ = make_engine(slots=(1, 3))

    async def scenario():
        await eng.start()

    asyncio.run(scenario())
    adv = [m for m in sent_json if m["type"] == "advertise"][-1]
    ids = {s["stream_id"] for s in adv["streams"]}
    assert ids == {"slot1.rx", "slot3.rx", "op.mic"}
    slot1 = next(s for s in adv["streams"] if s["stream_id"] == "slot1.rx")
    assert slot1["format"] == {"rate": 8000, "channels": 1}


def test_source_subscribe_starts_rx_bridge_and_frames_flow():
    nodes = {(1, "rx"): "oe5xrx.slot1"}
    eng, factory, sent_json, sent_bin = make_engine(slots=(1,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")

    asyncio.run(scenario())
    assert len(factory.rx) == 1
    rx = factory.rx[0]
    assert rx.node == "oe5xrx.slot1" and rx.rate == 8000 and rx.started

    # simulate an Opus packet from the bridge → a §5.3 frame is emitted with slot1.rx's ref
    rx.on_opus(b"\xfc" * 40)
    assert len(sent_bin) == 1
    mf = frame.parse_frame(sent_bin[0])
    assert mf.stream_ref == eng.registry.ref_for("slot1.rx")
    assert mf.payload == b"\xfc" * 40
    assert mf.dtx is False
    # a stream_state live was announced
    assert any(m["type"] == "stream_state" and m["state"] == "live" for m in sent_json)


def test_dtx_flag_set_on_tiny_comfort_packet():
    nodes = {(1, "rx"): "n"}
    eng, factory, _, sent_bin = make_engine(slots=(1,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")

    asyncio.run(scenario())
    factory.rx[0].on_opus(b"\x00")  # ≤2 bytes → Opus DTX comfort frame
    assert frame.parse_frame(sent_bin[0]).dtx is True


def test_unsubscribe_stops_bridge_and_marks_idle():
    nodes = {(1, "rx"): "n"}
    eng, factory, sent_json, _ = make_engine(slots=(1,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        await eng.on_source_unsubscribe("slot1.rx")

    asyncio.run(scenario())
    assert factory.rx[0].stopped
    assert any(m["type"] == "stream_state" and m["state"] == "idle" for m in sent_json)


def test_unknown_stream_subscribe_emits_error():
    eng, factory, sent_json, _ = make_engine(slots=(1,))

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot9.rx")

    asyncio.run(scenario())
    assert not factory.rx
    assert any(m["type"] == "error" and m["code"] == "unknown_stream" for m in sent_json)


def test_mic_state_active_starts_tx_and_media_is_injected():
    nodes = {(2, "tx"): "oe5xrx.slot2.tx"}
    eng, factory, _, _ = make_engine(slots=(2,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        # a media frame addressed to op.mic → fed into the TX bridge
        ref = eng.registry.mic_ref
        f = frame.pack_frame(stream_ref=ref, seq=0, ts=0, flags=0, payload=b"mic-opus")
        await eng.on_media_frame(f)

    asyncio.run(scenario())
    assert len(factory.tx) == 1
    tx = factory.tx[0]
    assert tx.node == "oe5xrx.slot2.tx" and tx.started
    assert tx.fed == [b"mic-opus"]


def test_mic_state_inactive_stops_tx():
    nodes = {(2, "tx"): "n.tx"}
    eng, factory, _, _ = make_engine(slots=(2,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        await eng.on_mic_state(active=False, tx_slot=None, tx_module=None)

    asyncio.run(scenario())
    assert factory.tx[0].stopped


def test_dead_man_tears_down_tx_on_silence():
    nodes = {(2, "tx"): "n.tx"}
    eng, factory, _, _ = make_engine(slots=(2,), nodes=nodes, dead_man=0.05)

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        await asyncio.sleep(0.15)  # no mic frames arrive → dead-man fires

    asyncio.run(scenario())
    assert factory.tx[0].stopped


def test_media_frame_before_mic_active_is_dropped():
    eng, factory, _, _ = make_engine(slots=(2,))

    async def scenario():
        await eng.start()
        f = frame.pack_frame(stream_ref=eng.registry.mic_ref, seq=0, ts=0, flags=0, payload=b"x")
        await eng.on_media_frame(f)  # no TX bridge → dropped, no crash

    asyncio.run(scenario())
    assert not factory.tx


def test_tot_force_stops_tx_even_with_continuous_media():
    nodes = {(2, "tx"): "n.tx"}
    eng, factory, _, _ = make_engine(slots=(2,), nodes=nodes, dead_man=10.0)
    eng._max_tx_seconds = 0.05  # tiny absolute ceiling

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot=2, tx_module="fm")
        # keep feeding media so the dead-man never fires; the TOT must still tear down
        ref = eng.registry.mic_ref
        for _ in range(3):
            f = frame.pack_frame(stream_ref=ref, seq=0, ts=0, flags=0, payload=b"x")
            await eng.on_media_frame(f)
            await asyncio.sleep(0.03)

    asyncio.run(scenario())
    assert factory.tx[0].stopped


def test_mic_state_rejects_non_int_slot():
    eng, factory, _, _ = make_engine(slots=(2,))

    async def scenario():
        await eng.start()
        await eng.on_mic_state(active=True, tx_slot="2", tx_module="fm")  # str, not int
        await eng.on_mic_state(active=True, tx_slot=True, tx_module="fm")  # bool, not int

    asyncio.run(scenario())
    assert not factory.tx  # neither malformed slot started a TX bridge


def test_late_rx_callback_after_unsubscribe_is_dropped():
    nodes = {(1, "rx"): "n"}
    eng, factory, _, sent_bin = make_engine(slots=(1,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        await eng.on_source_unsubscribe("slot1.rx")

    asyncio.run(scenario())
    # a reader-thread callback that fires after unsubscribe must not emit a frame
    factory.rx[0].on_opus(b"\xfc" * 20)
    assert sent_bin == []


def test_tx_route_is_stored():
    eng, _, _, _ = make_engine(slots=(1, 3))

    async def scenario():
        await eng.start()
        await eng.set_tx_route(3, "fm")

    asyncio.run(scenario())
    assert eng.tx_route == {"slot": 3, "module": "fm"}


def test_stop_tears_down_all_bridges():
    nodes = {(1, "rx"): "n", (1, "tx"): "n.tx"}
    eng, factory, _, _ = make_engine(slots=(1,), nodes=nodes)

    async def scenario():
        await eng.start()
        await eng.on_source_subscribe("slot1.rx")
        await eng.on_mic_state(active=True, tx_slot=1, tx_module="fm")
        await eng.stop()

    asyncio.run(scenario())
    assert factory.rx[0].stopped and factory.tx[0].stopped
