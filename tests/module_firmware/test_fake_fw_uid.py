from station_agent.slot_discovery import discover_slots
from tests.fake_fw import FakeFirmware, make_slot_tree

FM_SPEC = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm", "model": "sim", "version": "0.0.1"},
    "capabilities": [],
}


def test_fake_fw_injects_synthetic_uid(tmp_path):
    """A module spec without a uid gets a stable synthetic UID through discovery."""
    fw = FakeFirmware({"fm": FM_SPEC})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        entries = discover_slots(base, timeout=3.0)
        identity = entries[0]["modules"][0]["identity"]
        assert identity["uid"] == "SIM-FM-0001"
        assert identity["uid_source"] == "synthetic"
    finally:
        fw.stop()


def test_fake_fw_preserves_explicit_uid(tmp_path):
    """An explicit uid in the spec flows through verbatim (contract check)."""
    spec = {
        **FM_SPEC,
        "identity": {
            "type": "fm",
            "model": "sim",
            "version": "0.0.1",
            "uid": "REAL-UID-42",
            "uid_source": "stm32_uid",
        },
    }
    fw = FakeFirmware({"fm": spec})
    fw.start()
    try:
        base = make_slot_tree(tmp_path, {1: fw})
        entries = discover_slots(base, timeout=3.0)
        identity = entries[0]["modules"][0]["identity"]
        assert identity["uid"] == "REAL-UID-42"
        assert identity["uid_source"] == "stm32_uid"
    finally:
        fw.stop()
