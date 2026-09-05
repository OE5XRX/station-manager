# tests/test_audio_ws_client.py
"""AudioClient vs a fake server speaking the §5 contract (analog to test_control_client)."""

import asyncio
import json
import time

import pytest

pytest.importorskip("websockets")
import websockets

from station_agent.audio import frame
from station_agent.audio.ws_client import AudioClient


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
    def __init__(self, slots, nodes):
        self._slots, self._nodes = slots, nodes

    def list_audio_slots(self):
        return list(self._slots)

    def resolve_node(self, slot, direction):
        return self._nodes.get((slot, direction))


class FakeRx:
    def __init__(self, node, rate, on_opus):
        self.node, self.rate, self.on_opus = node, rate, on_opus
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeTx:
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


def test_full_contract_advertise_subscribe_media_and_mic(tmp_path):
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
                # demand-gate slot1.rx on
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
                # once the server subscribes, the engine builds a FakeRx we can drive
                assert await loop.run_in_executor(None, _wait_for, lambda: len(factory.rx) == 1)
                rx = factory.rx[0]
                assert rx.node == "oe5xrx.slot1" and rx.started
                # simulate an Opus packet from the bridge → a §5.3 media frame reaches the server
                rx.on_opus(b"\xfc" * 30)
                await asyncio.wait_for(got_media.wait(), timeout=5.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    asyncio.run(scenario())

    adv = received["advertise"]
    assert {s["stream_id"] for s in adv["streams"]} == {"slot1.rx", "op.mic"}
    assert received["media"], "no media frame relayed"
    mf = frame.parse_frame(received["media"][0])
    assert mf.payload == b"\xfc" * 30
    assert any(m["state"] == "live" for m in received["stream_state"])


def test_mic_state_and_inbound_media_reach_tx_bridge(tmp_path):
    key_path = _gen_key(tmp_path)
    backend = FakeBackend(slots=(2,), nodes={(2, "tx"): "oe5xrx.slot2.tx"})
    factory = FakeFactory()

    async def server(ws):
        first = True
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            msg = json.loads(raw)
            if msg["type"] == "advertise" and first:
                first = False
                # advertise carries the explicit stream_ref (§5.3), so the server knows
                # which ref op.mic media must use.
                mic_ref = next(
                    s["stream_ref"] for s in msg["streams"] if s["stream_id"] == "op.mic"
                )
                mic_on = {
                    "v": 1,
                    "type": "mic_state",
                    "active": True,
                    "tx_slot": 2,
                    "tx_module": "fm",
                }
                await ws.send(json.dumps(mic_on))
                await ws.send(
                    frame.pack_frame(stream_ref=mic_ref, seq=0, ts=0, flags=0, payload=b"mic-opus")
                )

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path)
            client = AudioClient(cfg, backend=backend, bridge_factory=factory)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                assert await loop.run_in_executor(None, _wait_for, lambda: len(factory.tx) == 1)
                tx = factory.tx[0]
                assert tx.node == "oe5xrx.slot2.tx" and tx.started
                # the op.mic media frame is decoded-and-injected into the TX bridge
                assert await loop.run_in_executor(None, _wait_for, lambda: tx.fed == [b"mic-opus"])
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    asyncio.run(scenario())
