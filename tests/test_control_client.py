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


# --- passive decoder host wiring ------------------------------------------------------


class _FakeUnits:
    def __init__(self):
        self.calls = []

    def start(self, decoder, slot, env):
        self.calls.append(("start", decoder, slot))

    def stop(self, decoder, slot):
        self.calls.append(("stop", decoder, slot))

    def state(self, decoder, slot):
        return "active"

    def stop_all(self):
        self.calls.append(("stop_all",))


class _TriggerPlugin:
    """Emits one reliable output each time ``trigger`` is set (from the test)."""

    name = "aprs"

    def __init__(self):
        self.trigger = None

    def env(self, slot, params, rx_node):
        return {"OE5XRX_SLOT": str(slot)}

    async def run(self, slot, emit):
        self.loop = asyncio.get_running_loop()
        self.trigger = self.trigger or asyncio.Event()
        await self.trigger.wait()
        await emit("ax25", True, {"ax25": "AA=="})
        await asyncio.Event().wait()


def _host_factory(units, plugin):
    from station_agent.decoders.host import DecoderHost

    return lambda: DecoderHost(units, {"aprs": plugin}, lambda slot: "node.rx")


def _run_client(tmp_path, server, cfg_mut, client_kw, until, fw_setup=None):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    key_path = _gen_key(tmp_path)

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            cfg_mut(cfg)
            client = ControlClient(cfg, **client_kw)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(until.wait(), timeout=15.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=8.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()
    return base


def test_inventory_advertises_decoders_feature_and_snapshot(tmp_path):
    msgs = []
    done = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if msg["type"] == "decoder_snapshot":
                done.set()

    units = _FakeUnits()
    _run_client(
        tmp_path,
        server,
        lambda cfg: None,
        {"decoder_host_factory": _host_factory(units, _TriggerPlugin())},
        done,
    )
    assert msgs[0]["type"] == "inventory" and msgs[0]["features"] == ["decoders"]
    assert msgs[1] == {"v": 1, "type": "decoder_snapshot", "decoders": []}
    assert units.calls[0] == ("stop_all",)


def test_no_features_without_host(tmp_path):
    msgs = []
    done = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msgs.append(json.loads(raw))
            done.set()

    _run_client(tmp_path, server, lambda cfg: None, {}, done)
    assert msgs[0]["type"] == "inventory" and "features" not in msgs[0]


def test_decoders_survive_ws_reconnect_and_resend(tmp_path):
    plugin = _TriggerPlugin()
    units = _FakeUnits()
    conns = []
    done = asyncio.Event()
    first_closed = asyncio.Event()

    async def server(ws):
        conn = {"msgs": []}
        conns.append(conn)
        idx = len(conns)
        async for raw in ws:
            msg = json.loads(raw)
            conn["msgs"].append(msg)
            if idx == 1 and msg["type"] == "decoder_snapshot":
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "decoder_start",
                            "decoder": "aprs",
                            "slot": 1,
                            "params": {},
                            "request_id": "d1",
                        }
                    )
                )
            elif idx == 1 and msg["type"] == "decoder_state" and msg["state"] == "running":
                await ws.close()
                first_closed.set()
                return
            elif idx == 2 and msg["type"] == "decoder_output":
                done.set()

    async def trigger_after_close():
        await first_closed.wait()
        await asyncio.sleep(0.2)
        # Thread-safe: the plugin lives on the client's loop (executor thread).
        plugin.loop.call_soon_threadsafe(plugin.trigger.set)  # WS is down -> spooled

    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    key_path = _gen_key(tmp_path)

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            client = ControlClient(cfg, decoder_host_factory=_host_factory(units, plugin))
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            trig = asyncio.create_task(trigger_after_close())
            try:
                await asyncio.wait_for(done.wait(), timeout=20.0)
            finally:
                trig.cancel()
                client.stop()
                await asyncio.wait_for(t, timeout=8.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    second = [m["type"] for m in conns[1]["msgs"]]
    assert second[:2] == ["inventory", "decoder_snapshot"]
    snap = conns[1]["msgs"][1]
    assert snap["decoders"] == [{"decoder": "aprs", "slot": 1, "state": "running"}]
    out = [m for m in conns[1]["msgs"] if m["type"] == "decoder_output"][0]
    assert out["seq"] == 1 and out["reliable"] is True
    assert ("stop", "aprs", 1) in units.calls  # stopped only by the final client shutdown
    assert units.calls.count(("start", "aprs", 1)) == 1


def test_rediscovery_module_gone_stops_slot_decoder(tmp_path):
    import os as _os

    plugin = _TriggerPlugin()
    units = _FakeUnits()
    done = asyncio.Event()
    base_holder = {}

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "decoder_snapshot":
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "decoder_start",
                            "decoder": "aprs",
                            "slot": 1,
                            "params": {},
                            "request_id": "d1",
                        }
                    )
                )
            elif msg["type"] == "decoder_state" and msg["state"] == "running":
                # Unplug the module: control symlink disappears -> probe misses.
                _os.unlink(_os.path.join(base_holder["base"], "slot1", "control"))
            elif msg["type"] == "decoder_state" and msg["state"] == "stopped":
                assert msg["detail"] == "module_gone"
                done.set()

    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    base_holder["base"] = base
    key_path = _gen_key(tmp_path)

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            cfg.control_rediscovery_interval = 0.3
            client = ControlClient(cfg, decoder_host_factory=_host_factory(units, plugin))
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(done.wait(), timeout=20.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=8.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()
    assert ("stop", "aprs", 1) in units.calls


def test_reconnect_does_not_stop_decoder_when_first_scan_misses(tmp_path):
    """A reconnect's first (undebounced) scan may miss a slot; decoders must survive."""
    import os as _os

    plugin = _TriggerPlugin()
    units = _FakeUnits()
    conns = []
    done = asyncio.Event()
    base_holder = {}

    async def server(ws):
        conn = {"msgs": []}
        conns.append(conn)
        idx = len(conns)
        async for raw in ws:
            msg = json.loads(raw)
            conn["msgs"].append(msg)
            if idx == 1 and msg["type"] == "decoder_snapshot":
                await ws.send(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "decoder_start",
                            "decoder": "aprs",
                            "slot": 1,
                            "params": {},
                            "request_id": "d1",
                        }
                    )
                )
            elif idx == 1 and msg["type"] == "decoder_state" and msg["state"] == "running":
                # Hide the module only for the next connection's first scan, then close.
                ctl = _os.path.join(base_holder["base"], "slot1", "control")
                base_holder["target"] = _os.readlink(ctl)
                _os.unlink(ctl)
                await ws.close()
                return
            elif idx == 2 and msg["type"] == "decoder_snapshot":
                # Module reappears right after the (empty) first scan.
                _os.symlink(
                    base_holder["target"], _os.path.join(base_holder["base"], "slot1", "control")
                )
                await asyncio.sleep(1.5)
                done.set()

    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = make_slot_tree(tmp_path, {1: fw})
    base_holder["base"] = base
    key_path = _gen_key(tmp_path)

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            cfg.control_rediscovery_interval = 0.3
            client = ControlClient(cfg, decoder_host_factory=_host_factory(units, plugin))
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(done.wait(), timeout=25.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=8.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    second = conns[1]["msgs"]
    assert second[0]["type"] == "inventory" and second[0]["slots"] == []  # scan missed
    assert second[1]["decoders"] == [{"decoder": "aprs", "slot": 1, "state": "running"}]
    states = [m["state"] for m in second if m["type"] == "decoder_state"]
    assert "stopped" not in states
    assert units.calls.count(("start", "aprs", 1)) == 1
