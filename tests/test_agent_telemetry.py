# tests/test_agent_telemetry.py
from unittest import mock

from station_agent import telemetry
from station_agent.config import AgentConfig


def _cfg(tmp_path):
    return AgentConfig(server_url="https://x", station_id=1,
                       ed25519_key_path="/k.pem", state_dir=str(tmp_path))


def test_collect_telemetry_all_sources_present(tmp_path):
    with mock.patch("station_agent.telemetry.bootinfo.detect_boot",
                    return_value={"boot_id": "b", "boot_count": 2, "reboot_reason": "clean"}), \
         mock.patch("station_agent.telemetry.power.read_throttle",
                    return_value={"throttled_hex": "0x0", "undervoltage_now": False}), \
         mock.patch("station_agent.telemetry.storage.read_storage_health",
                    return_value={"root_device": "mmcblk0", "devices": []}), \
         mock.patch("station_agent.telemetry._collect_slot",
                    return_value={"active_slot": "a", "image_version": "v1"}):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    assert result["boot"]["reboot_reason"] == "clean"
    assert result["power"]["throttled_hex"] == "0x0"
    assert result["storage"]["root_device"] == "mmcblk0"
    assert result["slot"]["active_slot"] == "a"


def test_collect_telemetry_graceful_when_sources_missing(tmp_path):
    with mock.patch("station_agent.telemetry.bootinfo.detect_boot",
                    side_effect=RuntimeError("boom")), \
         mock.patch("station_agent.telemetry.power.read_throttle", return_value=None), \
         mock.patch("station_agent.telemetry.storage.read_storage_health", return_value=None), \
         mock.patch("station_agent.telemetry._collect_slot", return_value={}):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    # No exception; absent sources omitted.
    assert "power" not in result
    assert "storage" not in result
    assert "boot" not in result
