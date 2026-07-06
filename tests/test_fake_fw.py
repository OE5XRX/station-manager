# tests/test_fake_fw.py
import os

from station_agent import slot_discovery
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
        {"name": "ptt", "kind": "action", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
    ],
}


def _send(path, line):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, (line + "\r\n").encode())
        import select

        buf = b""
        while (
            b"MODULE-RESULT" not in buf
            and b"MODULE-DESCRIBE" not in buf
            and b"MODULE-LIST" not in buf
        ):
            r, _, _ = select.select([fd], [], [], 2.0)
            if not r:
                break
            buf += os.read(fd, 4096)
        return buf.decode(errors="replace")
    finally:
        os.close(fd)


def test_fake_fw_lists_and_describes(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        assert "MODULE-LIST" in _send(fw.control_path, "module list")
        assert "fm_transceiver" in _send(fw.control_path, "module fm describe")
    finally:
        fw.stop()


def test_fake_fw_set_updates_state_and_get_reads_it(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        out = _send(fw.control_path, "module fm set frequency 145.5")
        assert '"ok":true' in out.replace(" ", "")
        assert fw.state["fm"]["frequency"] == "145.5"
        got = _send(fw.control_path, "module fm get frequency")
        assert "145.5" in got
    finally:
        fw.stop()


def test_make_slot_tree_discoverable(tmp_path):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        slots = slot_discovery.discover_slots(base, timeout=3.0)
        assert slots and slots[0]["slot"] == 1
        assert slots[0]["modules"][0]["id"] == "fm"
    finally:
        fw.stop()
