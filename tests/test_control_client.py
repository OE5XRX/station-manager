# tests/test_control_client.py
import asyncio
import json

import pytest

pytest.importorskip("websockets")
import websockets

from station_agent.control_client import ControlClient
from tests.fake_fw import FakeFirmware, make_slot_tree

FM = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {
            "name": "frequency",
            "kind": "setting",
            "type": "float",
            "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
        },
        {
            "name": "rssi",
            "kind": "telemetry",
            "type": "int",
            "readonly": True,
            "min_interval_ms": 250,
        },
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
        self.control_rediscovery_interval = 30.0


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
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "command",
                            "request_id": "c1",
                            "slot": 1,
                            "module": "fm",
                            "capability": "frequency",
                            "op": "set",
                            "value": 145.5,
                        }
                    )
                )
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


def test_control_client_rediscovers_and_reemits_inventory(tmp_path):
    import os as _os

    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = str(tmp_path / "oe5xrx")
    _os.makedirs(base, exist_ok=True)  # empty at connect time
    key_path = _gen_key(tmp_path)

    inventories = []
    second = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "inventory":
                inventories.append(msg)
                if len(inventories) == 1:
                    # After the first (empty) inventory, plug the module in.
                    slot_dir = _os.path.join(base, "slot1")
                    _os.makedirs(slot_dir, exist_ok=True)
                    _os.symlink(fw.control_path, _os.path.join(slot_dir, "control"))
                elif len(inventories) >= 2 and msg["slots"]:
                    second.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            cfg.control_rediscovery_interval = 0.5
            client = ControlClient(cfg)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(second.wait(), timeout=10.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    assert inventories[0]["slots"] == []
    assert any(s["slot"] == 1 for s in inventories[-1]["slots"])


def test_ws_send_on_closed_connection_does_not_raise(tmp_path):
    """_ws_send must swallow ConnectionClosed and not propagate to the caller."""
    key_path = _gen_key(tmp_path)
    cfg = _FakeConfig("http://127.0.0.1:9", 1, key_path, str(tmp_path))
    client = ControlClient(cfg)

    async def scenario():
        # Stub _ws with an object whose send() raises ConnectionClosed.
        class _ClosedWS:
            async def send(self, _data):
                raise websockets.exceptions.ConnectionClosed(
                    websockets.frames.Close(1001, "going away"), None
                )

        client._ws = _ClosedWS()
        # Must not raise; must clear _ws.
        await client._ws_send({"type": "test"})
        assert client._ws is None

    asyncio.run(scenario())
