import logging

from station_agent import serial_trace
from tests.fake_fw import FakeFirmware


def test_hexdump_formats_direction_length_and_hex():
    line = serial_trace.hexdump("TX", b"abc")
    assert "TX" in line
    assert "3" in line          # length
    assert "616263" in line     # hex of "abc"


def test_log_io_emits_only_when_enabled(caplog):
    log = logging.getLogger("station_agent.test")
    with caplog.at_level(logging.DEBUG, logger="station_agent.test"):
        serial_trace.log_io(log, "RX", b"xy", enabled=False)
        assert not caplog.records
        serial_trace.log_io(log, "RX", b"xy", enabled=True)
        assert any("7879" in r.getMessage() for r in caplog.records)


def test_discover_slots_forwards_trace_to_probe(tmp_path, monkeypatch):
    # trace_serial is useless unless discover_slots (the production heartbeat path)
    # actually forwards the flag down to probe_slot.
    from station_agent import slot_discovery

    (tmp_path / "slot0").mkdir()
    (tmp_path / "slot0" / "control").write_text("")
    seen = {}

    def fake_probe(control, timeout=3.0, trace=False):
        seen["trace"] = trace
        return []

    monkeypatch.setattr(slot_discovery, "probe_slot", fake_probe)
    slot_discovery.discover_slots(str(tmp_path), trace=True)
    assert seen["trace"] is True


def test_probe_slot_trace_emits_tx_rx(caplog):
    from station_agent import slot_discovery
    fw = FakeFirmware({"fm1": {"identity": {}, "capabilities": []}})
    fw.start()
    try:
        with caplog.at_level("DEBUG", logger="station_agent.slot_discovery"):
            slot_discovery.probe_slot(fw.control_path, timeout=3.0, trace=True)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "serial TX" in msgs and "serial RX" in msgs
    finally:
        fw.stop()
