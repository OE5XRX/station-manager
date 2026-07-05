# tests/test_broker.py
import asyncio
from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM = {
    "schema": 1, "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float",
         "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
    ],
}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _broker_with_fw(fw):
    col = Collector()
    b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0), now=lambda: 100.0)
    b.set_inventory([{"slot": 1, "control": fw.control_path,
                      "modules": [{"id": "fm", "identity": FM["identity"],
                                   "capabilities": FM["capabilities"]}]}])
    return b, col


def _run(coro):
    return asyncio.run(coro)


def test_command_set_valid_flows_to_fw_and_reports_result_and_state():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r1",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 145.5}))
        results = [m for m in col.sent if m["type"] == "result"]
        states = [m for m in col.sent if m["type"] == "state"]
        assert results[0]["request_id"] == "r1" and results[0]["ok"] is True
        assert states and states[0]["values"]["frequency"] is not None
        assert fw.state["fm"]["frequency"] == "145.5"
    finally:
        fw.stop()


def test_command_out_of_range_rejected_before_fw():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r2",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 200.0}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "out_of_range"
        # Never reached the firmware.
        assert "frequency" not in fw.state["fm"]
    finally:
        fw.stop()


def test_command_wrong_op_rejected_before_fw():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r3",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "do", "value": 145.5}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is False and res["error"]["code"] == "wrong_op"
    finally:
        fw.stop()


def test_command_unknown_slot():
    coll = Collector()
    b2 = Broker(coll, now=lambda: 1.0)
    b2.set_inventory([])
    _run(b2.handle({"v": 1, "type": "command", "request_id": "r4",
                    "slot": 9, "module": "fm", "capability": "rssi", "op": "get"}))
    res = [m for m in coll.sent if m["type"] == "result"][0]
    assert res["ok"] is False and res["error"]["code"] == "unknown_slot"


def test_command_get_reports_value():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        _run(b.handle({"v": 1, "type": "command", "request_id": "r5",
                       "slot": 1, "module": "fm", "capability": "rssi", "op": "get"}))
        res = [m for m in col.sent if m["type"] == "result"][0]
        assert res["ok"] is True and isinstance(res["value"], int)
    finally:
        fw.stop()


def test_emit_inventory_includes_descriptors_and_settings_snapshot():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        b, col = _broker_with_fw(fw)
        # Seed a setting so the snapshot has something to read back.
        _run(b.handle({"v": 1, "type": "command", "request_id": "r0",
                       "slot": 1, "module": "fm", "capability": "frequency",
                       "op": "set", "value": 145.5}))
        col.sent.clear()
        _run(b.emit_inventory())
        inv = [m for m in col.sent if m["type"] == "inventory"][0]
        slot = inv["slots"][0]
        assert slot["slot"] == 1
        mod = slot["modules"][0]
        assert mod["module"] == "fm"
        assert any(c["name"] == "frequency" for c in mod["capabilities"])
        # frequency (a setting) is in the snapshot; rssi (telemetry) is not.
        assert "frequency" in mod["state"]
        assert "rssi" not in mod["state"]
    finally:
        fw.stop()
