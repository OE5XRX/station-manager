# tests/test_control_client.py
import asyncio
import json
import pytest

pytest.importorskip("websockets")
import websockets

from station_agent.control_client import ControlClient
from tests.fake_fw import FakeFirmware, make_slot_tree

FM = {
    "schema": 1, "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
    ],
}


class _FakeConfig:
    def __init__(self, server_url, station_id, key_path, slot_base):
        self.server_url = server_url
        self.station_id = station_id
        self.ed25519_key_path = key_path
        self.slot_dev_base = slot_base
        self.slot_discovery_enabled = True
        self.control_dead_man_timeout = 1.5
        self.telemetry_default_interval_ms = 1000
        self.telemetry_min_floor_ms = 200


def _gen_key(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    p = tmp_path / "agent.key"
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    return str(p)


def test_control_client_connects_sends_inventory_and_handles_command(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    key_path = _gen_key(tmp_path)

    received = {"inventory": None, "result": None, "state": None}
    done = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "inventory" and received["inventory"] is None:
                received["inventory"] = msg
                await ws.send(json.dumps({"v": 1, "type": "command", "request_id": "c1",
                                          "slot": 1, "module": "fm",
                                          "capability": "frequency", "op": "set", "value": 145.5}))
            elif msg["type"] == "result":
                received["result"] = msg
            elif msg["type"] == "state":
                received["state"] = msg
                done.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            client = ControlClient(cfg)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(done.wait(), timeout=8.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    assert received["inventory"]["slots"][0]["slot"] == 1
    assert received["result"]["ok"] is True and received["result"]["request_id"] == "c1"
    assert "frequency" in received["state"]["values"]
    assert fw.state["fm"]["frequency"] == "145.5"
