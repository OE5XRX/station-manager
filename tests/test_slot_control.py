# tests/test_slot_control.py
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM = {
    "schema": 1, "module": "fm",
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
    ],
}


def test_execute_set_returns_module_result():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "set", "frequency", "145.5")
        assert r["ok"] is True
        assert r["cap"] == "frequency" and r["op"] == "set"
        assert fw.state["fm"]["frequency"] == "145.5"
    finally:
        fw.stop()


def test_execute_get_reads_value():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "get", "rssi")
        assert r["ok"] is True and isinstance(r["value"], int)
    finally:
        fw.stop()


def test_execute_unknown_capability_passes_through_fw_error():
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        sc = SlotControl(fw.control_path, timeout=2.0)
        r = sc.execute("fm", "set", "banana", "1")
        assert r["ok"] is False and r["error"] == "unknown_capability"
    finally:
        fw.stop()


def test_execute_timeout_on_dead_path(tmp_path):
    missing = str(tmp_path / "control")
    sc = SlotControl(missing, timeout=0.3)
    r = sc.execute("fm", "get", "rssi")
    assert r["ok"] is False and r["error"] == "timeout"
