# tests/test_broker_generic.py
import asyncio
from station_agent.broker import Broker
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

# A totally different, invented module — NOT "fm". Same generic contract.
BEACON = {
    "schema": 1, "module": "beacon",
    "identity": {"type": "beacon_tx", "model": "BCN-1", "version": "1"},
    "capabilities": [
        {"name": "interval", "kind": "setting", "type": "int", "ranges": [{"min": 1, "max": 60}]},
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "temperature", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 100},
    ],
}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def test_second_module_flows_through_broker_unchanged(tmp_path):
    fw = FakeFirmware({"beacon": BEACON})
    fw.start()
    try:
        col = Collector()
        b = Broker(col, transport_factory=lambda p: SlotControl(p, timeout=2.0),
                   dead_man_timeout=0.1, telemetry_min_floor_ms=10,
                   telemetry_default_interval_ms=20, now=lambda: 1.0)
        b.set_inventory([{"slot": 2, "control": fw.control_path,
                          "modules": [{"id": "beacon", "identity": BEACON["identity"],
                                       "capabilities": BEACON["capabilities"]}]}])

        async def scenario():
            # 1) inventory
            await b.emit_inventory()
            # 2) valid set + 3) range reject, both without any 'fm'/'beacon' special-casing
            await b.handle({"v": 1, "type": "command", "request_id": "g1", "slot": 2,
                            "module": "beacon", "capability": "interval", "op": "set", "value": 10})
            await b.handle({"v": 1, "type": "command", "request_id": "g2", "slot": 2,
                            "module": "beacon", "capability": "interval", "op": "set", "value": 99})
            # 4) telemetry subscription streams, then unsubscribe
            await b.handle_subscribe({"slot": 2, "module": "beacon",
                                      "capabilities": ["temperature"], "interval_ms": 20})
            await asyncio.sleep(0.1)
            await b.handle_unsubscribe({"slot": 2, "module": "beacon",
                                        "capabilities": ["temperature"]})
            # 5) PTT dead-man on the invented module's bool action
            await b.handle({"v": 1, "type": "command", "request_id": "g3", "slot": 2,
                            "module": "beacon", "capability": "ptt", "op": "do", "value": True})
            await asyncio.sleep(0.3)
            await b.stop()

        _run = asyncio.run
        _run(scenario())

        results = [m for m in col.sent if m["type"] == "result"]
        assert any(r["request_id"] == "g1" and r["ok"] for r in results)
        assert any(r["request_id"] == "g2" and not r["ok"]
                   and r["error"]["code"] == "out_of_range" for r in results)
        assert [m for m in col.sent if m["type"] == "state"]           # telemetry streamed
        assert any(m.get("event") == "ptt_auto_unkey" for m in col.sent)  # dead-man fired
        assert fw.state["beacon"]["ptt"] == "false"
    finally:
        fw.stop()
