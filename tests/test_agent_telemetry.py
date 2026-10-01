# tests/test_agent_telemetry.py
from unittest import mock

from station_agent import bootinfo, telemetry
from station_agent.config import AgentConfig


def _cfg(tmp_path):
    return AgentConfig(
        server_url="https://x", station_id=1, ed25519_key_path="/k.pem", state_dir=str(tmp_path)
    )


def test_collect_telemetry_all_sources_present(tmp_path):
    with (
        mock.patch(
            "station_agent.telemetry.bootinfo.detect_boot",
            return_value={"boot_id": "b", "boot_count": 2, "reboot_reason": "clean"},
        ),
        mock.patch(
            "station_agent.telemetry.power.read_throttle",
            return_value={"throttled_hex": "0x0", "undervoltage_now": False},
        ),
        mock.patch(
            "station_agent.telemetry.storage.read_storage_health",
            return_value={"root_device": "mmcblk0", "devices": []},
        ),
        mock.patch(
            "station_agent.telemetry._collect_slot",
            return_value={"active_slot": "a", "image_version": "v1"},
        ),
        mock.patch("station_agent.telemetry.hb.get_uptime", return_value=120.5),
    ):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    assert result["boot"]["reboot_reason"] == "clean"
    assert result["power"]["throttled_hex"] == "0x0"
    assert result["storage"]["root_device"] == "mmcblk0"
    assert result["slot"]["active_slot"] == "a"


def test_collect_telemetry_graceful_when_sources_missing(tmp_path):
    with (
        mock.patch(
            "station_agent.telemetry.bootinfo.detect_boot", side_effect=RuntimeError("boom")
        ),
        mock.patch("station_agent.telemetry.power.read_throttle", return_value=None),
        mock.patch("station_agent.telemetry.storage.read_storage_health", return_value=None),
        mock.patch("station_agent.telemetry._collect_slot", return_value={}),
    ):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    # No exception; absent sources omitted.
    assert "power" not in result
    assert "storage" not in result
    assert "boot" not in result


# M14: uptime_seconds must be included in the boot block
def test_collect_telemetry_includes_uptime_seconds(tmp_path):
    """M14: collect_telemetry populates boot.uptime_seconds from /proc/uptime."""
    with (
        mock.patch(
            "station_agent.telemetry.bootinfo.detect_boot",
            return_value={"boot_id": "b", "boot_count": 1, "reboot_reason": "clean"},
        ),
        mock.patch("station_agent.telemetry.power.read_throttle", return_value=None),
        mock.patch("station_agent.telemetry.storage.read_storage_health", return_value=None),
        mock.patch(
            "station_agent.telemetry._collect_slot",
            return_value={"active_slot": "a"},
        ),
        mock.patch("station_agent.telemetry.hb.get_uptime", return_value=999.0),
    ):
        result = telemetry.collect_telemetry(_cfg(tmp_path))
    assert result["boot"]["uptime_seconds"] == 999.0


# M13: _collect_slot reads last_ota_result.json when present
def test_collect_slot_reads_last_ota_result(tmp_path):
    """M13: if last_ota_result.json exists, slot.last_ota_result reflects it."""
    import station_agent.bootloader as bl_real

    bootinfo.write_last_ota_result(str(tmp_path), "success", version="v9")
    cfg = _cfg(tmp_path)

    with (
        mock.patch.object(bl_real, "get_bootloader", return_value=object()),
        mock.patch.object(bl_real, "get_active_slot", return_value="a"),
        mock.patch("station_agent.telemetry.inventory.get_current_version", return_value="v9"),
    ):
        result = telemetry._collect_slot(cfg)

    assert result["last_ota_result"] == "success"


def test_collect_slot_last_ota_result_absent(tmp_path):
    """M13: if last_ota_result.json is absent, slot.last_ota_result is None."""
    import station_agent.bootloader as bl_real

    cfg = _cfg(tmp_path)
    with (
        mock.patch.object(bl_real, "get_bootloader", return_value=object()),
        mock.patch.object(bl_real, "get_active_slot", return_value="a"),
        mock.patch("station_agent.telemetry.inventory.get_current_version", return_value="v1"),
    ):
        result = telemetry._collect_slot(cfg)
    assert result["last_ota_result"] is None
