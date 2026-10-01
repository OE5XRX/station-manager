# tests/test_agent_bootinfo_detect.py
import json
from unittest import mock

from station_agent import bootinfo


def test_detect_boot_first_time_records_without_reboot_event(tmp_path):
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"), \
         mock.patch.object(bootinfo, "_gather_evidence", return_value={}):
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
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-B"), \
         mock.patch.object(bootinfo, "_gather_evidence",
                           return_value={"pstore_crash": True}):
        result = bootinfo.detect_boot(str(tmp_path))
    assert result["boot_id"] == "boot-B"
    assert result["boot_count"] == 4
    assert result["reboot_reason"] == "crash"


def test_detect_boot_unwritable_state_dir_degrades():
    with mock.patch.object(bootinfo, "read_boot_id", return_value="boot-A"), \
         mock.patch.object(bootinfo, "_gather_evidence", return_value={}), \
         mock.patch("station_agent.bootinfo._load_state", side_effect=OSError), \
         mock.patch("station_agent.bootinfo._save_state", side_effect=OSError):
        result = bootinfo.detect_boot("/nonexistent/path")
    assert result["boot_count"] == 0
    assert result["reboot_reason"] == "unknown"


def test_clean_marker_roundtrip(tmp_path):
    assert bootinfo.clean_marker_present(str(tmp_path)) is False
    bootinfo.mark_clean_shutdown(str(tmp_path))
    assert bootinfo.clean_marker_present(str(tmp_path)) is True
