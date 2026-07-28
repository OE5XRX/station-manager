from station_agent import selftest
from station_agent.__main__ import main
from tests.fake_fw import FakeFirmware


def test_run_serial_returns_zero_when_module_describes(tmp_path):
    fw = FakeFirmware({"fm1": {"identity": {"model": "SA818"}, "capabilities": ["ptt"]}})
    fw.start()
    try:
        rc = selftest.run_serial(fw.control_path, timeout=3.0)
        assert rc == 0
    finally:
        fw.stop()


def test_run_serial_returns_one_on_dead_path(tmp_path):
    dead = str(tmp_path / "nonexistent")
    assert selftest.run_serial(dead, timeout=0.5) == 1


def test_cli_selftest_serial_dead_path_returns_one(tmp_path):
    rc = main(["selftest", "serial", "--base", str(tmp_path), "--slot", "9", "--timeout", "0.5"])
    assert rc == 1
