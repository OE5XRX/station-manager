# tests/test_agent_bootinfo_detect.py
import json
from unittest import mock

from station_agent import bootinfo


def test_detect_boot_first_time_records_without_reboot_event(tmp_path):
    with (
        mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"),
        mock.patch.object(bootinfo, "_gather_evidence", return_value={}),
    ):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_id"] == "boot-A"
    assert result["boot_count"] == 1
    state = json.loads((tmp_path / "boot_state.json").read_text())
    assert state["boot_id"] == "boot-A"
    assert state["boot_count"] == 1


def test_detect_boot_same_boot_id_no_increment(tmp_path):
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 3, "reboot_reason": "clean"})
    )
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_count"] == 3
    assert result["reboot_reason"] == "clean"


def test_detect_boot_new_boot_id_increments_and_classifies(tmp_path):
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 3, "reboot_reason": "clean"})
    )
    with (
        mock.patch.object(bootinfo, "read_boot_id", return_value="boot-B"),
        mock.patch.object(bootinfo, "_gather_evidence", return_value={"pstore_crash": True}),
    ):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_id"] == "boot-B"
    assert result["boot_count"] == 4
    assert result["reboot_reason"] == "crash"


def test_detect_boot_unwritable_state_dir_degrades():
    with (
        mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"),
        mock.patch.object(bootinfo, "_gather_evidence", return_value={}),
        mock.patch("station_agent.bootinfo._load_state", side_effect=OSError),
        mock.patch("station_agent.bootinfo._save_state", side_effect=OSError),
    ):
        result = bootinfo.detect_boot("/nonexistent/path")
    assert result["boot_count"] == 0
    assert result["reboot_reason"] == "unknown"


def test_detect_boot_gather_evidence_raises_does_not_propagate(tmp_path):
    with (
        mock.patch.object(bootinfo, "read_boot_id", return_value="boot-B"),
        mock.patch.object(
            bootinfo, "_gather_evidence", side_effect=RuntimeError("throttle read blew up")
        ),
    ):
        result = bootinfo.detect_boot(str(tmp_path))
    # Did not raise; degraded evidence -> "unknown", but still a new boot.
    assert result["boot_id"] == "boot-B"
    assert result["boot_count"] == 1
    assert result["reboot_reason"] == "unknown"
    state = json.loads((tmp_path / "boot_state.json").read_text())
    assert state["boot_count"] == 1


def test_clean_marker_roundtrip(tmp_path):
    assert bootinfo.clean_marker_present(str(tmp_path)) is False
    bootinfo.mark_clean_shutdown(str(tmp_path))
    assert bootinfo.clean_marker_present(str(tmp_path)) is True


# M10: boot_id unreadable must not fabricate a new reboot
def test_detect_boot_none_boot_id_does_not_increment(tmp_path):
    """M10: when boot_id is None, return existing state unchanged — no reboot manufacture."""
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 3, "reboot_reason": "clean"})
    )
    with mock.patch.object(bootinfo, "read_boot_id", return_value=None):
        result1 = bootinfo.detect_boot(str(tmp_path))
        result2 = bootinfo.detect_boot(str(tmp_path))
    # Count must not increment across repeated calls
    assert result1["boot_count"] == 3
    assert result2["boot_count"] == 3
    assert result1["boot_id"] is None
    assert result1["reboot_reason"] == "unknown"
    # State file must NOT be rewritten (would increment count)
    state = json.loads((tmp_path / "boot_state.json").read_text())
    assert state["boot_count"] == 3


def test_detect_boot_none_boot_id_no_existing_state_does_not_increment(tmp_path):
    """M10: no state file + None boot_id → boot_count stays 0, no writes."""
    with mock.patch.object(bootinfo, "read_boot_id", return_value=None):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_count"] == 0
    assert not (tmp_path / "boot_state.json").exists()


# M11: same-boot path must clear the clean marker
def test_detect_boot_same_boot_clears_clean_marker(tmp_path):
    """M11: on same-boot fast-path, clean marker is consumed so it can't stale-survive."""
    (tmp_path / "boot_state.json").write_text(
        json.dumps({"boot_id": "boot-A", "boot_count": 1, "reboot_reason": "clean"})
    )
    bootinfo.mark_clean_shutdown(str(tmp_path))
    assert bootinfo.clean_marker_present(str(tmp_path))
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"):
        bootinfo.detect_boot(str(tmp_path))
    assert not bootinfo.clean_marker_present(str(tmp_path))


# H2: pending_upgrade marker prevents false ota_rollback on ordinary reboots
def test_bootloader_rollback_requires_marker(tmp_path):
    """H2: without marker, _bootloader_rollback always returns False (no false positive)."""
    assert not bootinfo._bootloader_rollback(None, str(tmp_path))


def test_bootloader_rollback_with_marker_and_revert_tuple(tmp_path):
    """H2: marker present + bootloader tuple shows revert → True."""
    import station_agent.bootloader as bl_real

    bootinfo.mark_pending_upgrade(str(tmp_path), "b")

    def fake_get_env(bl, key):
        return {"upgrade_available": "0", "bootcount": "1"}[key]

    with mock.patch.object(bl_real, "get_env", side_effect=fake_get_env):
        result = bootinfo._bootloader_rollback(object(), str(tmp_path))
    assert result is True


def test_bootloader_rollback_marker_consumed_on_new_boot(tmp_path):
    """H2: detect_boot consumes the pending-upgrade marker after classification."""
    bootinfo.mark_pending_upgrade(str(tmp_path), "b")
    assert bootinfo._pending_upgrade_marker_present(str(tmp_path))
    with (
        mock.patch.object(bootinfo, "read_boot_id", return_value="boot-B"),
        mock.patch.object(bootinfo, "_gather_evidence", return_value={}),
    ):
        bootinfo.detect_boot(str(tmp_path))
    assert not bootinfo._pending_upgrade_marker_present(str(tmp_path))


# H3: watchdog detection uses journalctl -b -1 (prior boot), not current dmesg
def test_dmesg_watchdog_absent_journalctl_returns_false():
    """H3: journalctl absent → False (graceful degrade)."""
    with mock.patch("station_agent.bootinfo.shutil.which", return_value=None):
        assert bootinfo._dmesg_watchdog() is False


def test_dmesg_watchdog_prior_boot_reset_line_returns_true():
    """H3: prior-boot log with bcm2835-wdt + reset → True."""
    prior_log = "[1.0] bcm2835-wdt: Broadcom watchdog timer\n[2.0] watchdog: watchdog reset\n"
    with (
        mock.patch("station_agent.bootinfo.shutil.which", return_value="/usr/bin/journalctl"),
        mock.patch(
            "station_agent.bootinfo.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=prior_log),
        ),
    ):
        assert bootinfo._dmesg_watchdog() is True


def test_dmesg_watchdog_driver_registration_only_returns_false():
    """H3: prior-boot log with only driver registration (no reset context) → False."""
    prior_log = "[1.0] bcm2835-wdt: Broadcom watchdog timer loaded, period=15s\n"
    with (
        mock.patch("station_agent.bootinfo.shutil.which", return_value="/usr/bin/journalctl"),
        mock.patch(
            "station_agent.bootinfo.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=prior_log),
        ),
    ):
        assert bootinfo._dmesg_watchdog() is False


# M13: last_ota_result roundtrip
def test_write_and_read_last_ota_result(tmp_path):
    """M13: write_last_ota_result persists; read_last_ota_result retrieves it."""
    bootinfo.write_last_ota_result(str(tmp_path), "success", version="v9")
    data = bootinfo.read_last_ota_result(str(tmp_path))
    assert data["result"] == "success"
    assert data["version"] == "v9"


def test_read_last_ota_result_absent(tmp_path):
    """M13: absent file → None."""
    assert bootinfo.read_last_ota_result(str(tmp_path)) is None
