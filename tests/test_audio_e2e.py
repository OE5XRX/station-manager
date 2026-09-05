# tests/test_audio_e2e.py
"""Full-E2E audio path: a REAL 1 kHz Opus tone threaded through the real station_agent
audio pipeline against a fake §5 server.

Only the device/gst layer is faked (FakeBackend + a bridge_factory whose bridges do NOT
spawn gst). Everything else is production code: the real ``AudioClient`` (own thread +
asyncio loop, Ed25519 signed-query auth), the real ``AudioEngine`` + ``StreamRegistry`` +
§5.3 frame codec.

The tone is the raw Opus packet inside ``tests/fixtures/audio/media_frame_slot0rx.bin`` —
a §5.3 frame wrapping one real 1 kHz Opus packet. ``parse_frame(...).payload`` gives the
Opus bytes reused for BOTH the RX drive and the TX inject.

Header/round-trip assertions run everywhere. The Opus decode + 1 kHz Goertzel-peak check is
gated on ``pytest.importorskip("av")`` — a dev-box/CI convenience, never a station_agent
runtime dependency, so the test is green when PyAV is absent (as on this machine).
"""
import asyncio
import json
import pathlib
import struct
import time

import pytest

pytest.importorskip("websockets")
import websockets

from station_agent.audio import frame

FIX = pathlib.Path(__file__).parent / "fixtures" / "audio"

# The real 1 kHz Opus tone, extracted once from the §5.3 golden fixture.
TONE_OPUS = frame.parse_frame((FIX / "media_frame_slot0rx.bin").read_bytes()).payload


class _FakeConfig:
    def __init__(self, server_url, station_id, key_path):
        self.server_url = server_url
        self.station_id = station_id
        self.ed25519_key_path = key_path
        self.audio_rx_rate = 8000
        self.audio_mic_rate = 16000
        self.audio_dead_man_timeout = 1.5


def _gen_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    p = tmp_path / "agent.key"
    p.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(p)


class FakeBackend:
    """Stands in for PipeWireRouterBackend — no udev/pw-dump, just canned topology."""

    def __init__(self, slots, nodes):
        self._slots, self._nodes = slots, nodes

    def list_audio_slots(self):
        return list(self._slots)

    def resolve_node(self, slot, direction):
        return self._nodes.get((slot, direction))


class FakeRx:
    """RX bridge that does NOT spawn gst; the test drives ``on_opus`` directly."""

    def __init__(self, node, rate, on_opus):
        self.node, self.rate, self.on_opus = node, rate, on_opus
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeTx:
    """TX bridge that does NOT spawn gst; the test observes ``feed_opus``."""

    def __init__(self, node, rate):
        self.node, self.rate = node, rate
        self.fed = []
        self.started = False

    def start(self):
        self.started = True

    def feed_opus(self, payload):
        self.fed.append(payload)

    def stop(self):
        pass


class FakeFactory:
    def __init__(self):
        self.rx, self.tx = [], []

    def make_rx(self, node, rate, on_opus):
        b = FakeRx(node, rate, on_opus)
        self.rx.append(b)
        return b

    def make_tx(self, node, rate):
        b = FakeTx(node, rate)
        self.tx.append(b)
        return b


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _assert_tone_is_1khz(payload):
    """av-gated: decode the Opus payload (libopus) and assert a dominant 1 kHz peak.

    Returns ``False`` (no-op) when PyAV is absent so the caller still PASSES — the
    header/round-trip assertions are the machine-independent contract. Returns ``True``
    when the decode ran, so the caller can report whether the av path was exercised.
    """
    try:
        import av
    except ImportError:
        return False
    from station_agent.audio.goertzel import dominant_bin

    cc = av.CodecContext.create("libopus", "r")
    cc.sample_rate = 48000
    cc.format = "s16"
    cc.layout = "mono"
    samples = []
    rate = 48000
    for pkt in (av.Packet(payload), None):
        for fr in cc.decode(pkt):
            rate = fr.sample_rate
            pcm = bytes(fr.planes[0])[: fr.samples * 2]
            samples += list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
    assert samples, "opus payload decoded to no audio"
    assert dominant_bin(samples, [500, 1000, 1500, 2000, 3000], rate) == 1000
    return True


def test_rx_tone_flows_agent_to_server_as_media_frame(tmp_path):
    """RX E2E: server source_subscribe(slot1.rx) → engine starts an RX bridge → the test
    drives on_opus with the REAL 1 kHz Opus tone → a §5.3 media frame reaches the fake
    server → header magic/ver/stream_ref match slot1.rx's advertised ref and the payload
    round-trips byte-for-byte (and, if av present, decodes to a dominant 1 kHz peak).
    """
    from station_agent.audio.ws_client import AudioClient

    key_path = _gen_key(tmp_path)
    backend = FakeBackend(
        slots=(1,), nodes={(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"}
    )
    factory = FakeFactory()

    received = {"advertise": None, "media": [], "stream_state": []}
    got_media = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                received["media"].append(bytes(raw))
                got_media.set()
                continue
            msg = json.loads(raw)
            if msg["type"] == "advertise" and received["advertise"] is None:
                received["advertise"] = msg
                await ws.send(
                    json.dumps({"v": 1, "type": "source_subscribe", "stream_id": "slot1.rx"})
                )
            elif msg["type"] == "stream_state":
                received["stream_state"].append(msg)

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path)
            client = AudioClient(cfg, backend=backend, bridge_factory=factory)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                assert await loop.run_in_executor(
                    None, _wait_for, lambda: len(factory.rx) == 1
                ), "engine never built the RX bridge after source_subscribe"
                rx = factory.rx[0]
                assert rx.node == "oe5xrx.slot1" and rx.started
                # Drive the bridge with the REAL 1 kHz Opus tone → §5.3 media frame out.
                rx.on_opus(TONE_OPUS)
                await asyncio.wait_for(got_media.wait(), timeout=5.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    asyncio.run(scenario())

    # advertise established the stream_ref↔stream_id mapping the server must trust (§5.3).
    adv = received["advertise"]
    by_id = {s["stream_id"]: s for s in adv["streams"]}
    assert set(by_id) == {"slot1.rx", "op.mic"}
    slot1_ref = by_id["slot1.rx"]["stream_ref"]

    assert received["media"], "no §5.3 media frame relayed agent→server"
    raw = received["media"][0]
    assert isinstance(raw, bytes)
    # Header magic/ver check via the real parser (raises FrameError on mismatch).
    mf = frame.parse_frame(raw)
    assert raw[0] == frame.MAGIC and raw[1] == frame.VERSION
    # stream_ref carried in the frame header must equal slot1.rx's advertised ref.
    assert mf.stream_ref == slot1_ref
    # Payload round-trips byte-for-byte: the real Opus tone survived the whole path.
    assert mf.payload == TONE_OPUS
    # First packet after (re)subscribe is a talk-onset marker (engine sets it at seq 0).
    assert mf.marker
    assert any(m["state"] == "live" for m in received["stream_state"])

    # av-gated: prove it is genuinely a 1 kHz tone, not just opaque bytes.
    _assert_tone_is_1khz(mf.payload)


def test_tx_tone_flows_server_to_agent_into_tx_bridge(tmp_path):
    """TX E2E: server mic_state(active, tx_slot=2) → engine starts a TX bridge → server
    sends a §5.3 media frame with stream_ref = op.mic's advertised ref carrying the same
    REAL Opus tone → assert the TX bridge's feed_opus received exactly that payload
    (server→agent→TX-sink path), byte-for-byte (and, if av present, it is a 1 kHz tone).
    """
    from station_agent.audio.ws_client import AudioClient

    key_path = _gen_key(tmp_path)
    backend = FakeBackend(slots=(2,), nodes={(2, "tx"): "oe5xrx.slot2.tx"})
    factory = FakeFactory()
    captured = {"mic_ref": None}

    async def server(ws):
        first = True
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            msg = json.loads(raw)
            if msg["type"] == "advertise" and first:
                first = False
                # advertise carries the explicit stream_ref (§5.3); the server learns which
                # ref op.mic media must use rather than guessing.
                mic_ref = next(
                    s["stream_ref"] for s in msg["streams"] if s["stream_id"] == "op.mic"
                )
                captured["mic_ref"] = mic_ref
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "mic_state",
                            "active": True,
                            "tx_slot": 2,
                            "tx_module": "fm",
                        }
                    )
                )
                # Inject the REAL 1 kHz Opus tone as an op.mic §5.3 media frame.
                await ws.send(
                    frame.pack_frame(
                        stream_ref=mic_ref, seq=0, ts=0, flags=0, payload=TONE_OPUS
                    )
                )

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path)
            client = AudioClient(cfg, backend=backend, bridge_factory=factory)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                assert await loop.run_in_executor(
                    None, _wait_for, lambda: len(factory.tx) == 1
                ), "engine never built the TX bridge after mic_state(active)"
                tx = factory.tx[0]
                assert tx.node == "oe5xrx.slot2.tx" and tx.started
                # The op.mic media frame is parsed and its Opus payload injected into TX.
                assert await loop.run_in_executor(
                    None, _wait_for, lambda: tx.fed == [TONE_OPUS]
                ), "TX bridge never received the injected op.mic Opus tone"
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    asyncio.run(scenario())

    tx = factory.tx[0]
    assert captured["mic_ref"] is not None
    # Exactly one payload, byte-for-byte the injected tone (server→agent→TX-sink).
    assert tx.fed == [TONE_OPUS]

    # av-gated: prove the injected payload is genuinely the 1 kHz tone.
    _assert_tone_is_1khz(tx.fed[0])
