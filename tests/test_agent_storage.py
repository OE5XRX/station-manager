import os
from unittest import mock

from station_agent import storage

EXTCSD_SAMPLE = """
eMMC Life Time Estimation A [EXT_CSD_DEVICE_LIFE_TIME_EST_TYP_A]: 0x02
eMMC Life Time Estimation B [EXT_CSD_DEVICE_LIFE_TIME_EST_TYP_B]: 0x01
eMMC Pre EOL information [EXT_CSD_PRE_EOL_INFO]: 0x01
"""

DMESG_SAMPLE = """
[    1.23] mmc0: new HS400 MMC card
[ 9000.00] mmcblk0: error -110 transferring data
[ 9001.00] blk_update_request: I/O error, dev mmcblk0, sector 123
"""


def test_life_time_band_to_pct():
    assert storage.life_time_band_to_pct(0x01) == 10
    assert storage.life_time_band_to_pct(0x0A) == 100
    assert storage.life_time_band_to_pct(0x00) is None


def test_parse_extcsd():
    result = storage.parse_extcsd(EXTCSD_SAMPLE)
    assert result["life_time_a_pct"] == 20
    assert result["life_time_b_pct"] == 10
    assert result["pre_eol"] == "normal"


def test_parse_extcsd_pre_eol_urgent():
    result = storage.parse_extcsd("eMMC Pre EOL information [EXT_CSD_PRE_EOL_INFO]: 0x03\n")
    assert result["pre_eol"] == "urgent"


def test_count_mmc_io_errors():
    assert storage.count_mmc_io_errors(DMESG_SAMPLE) == 2


def test_count_mmc_io_errors_none():
    assert storage.count_mmc_io_errors("[0.0] clean boot\n") == 0


# M12: _root_block_device must use (major, minor) pair, not just major
def test_root_block_device_matches_by_minor(tmp_path):
    """M12: when two mmcblk devices share the same major, only the one with matching minor wins."""
    # Fake root stat: major=179, minor=2 (partition mmcblk0p2 on device mmcblk0)
    fake_stat = mock.Mock()
    fake_stat.st_dev = os.makedev(179, 2)

    # /sys/class/block contents: mmcblk0p1 (179:1), mmcblk0p2 (179:2), mmcblk1p1 (179:9)
    def fake_listdir(path):
        return ["mmcblk0p1", "mmcblk0p2", "mmcblk1p1"]

    def fake_exists(path):
        return path.endswith("/dev")

    def fake_open(path, encoding=None):
        devmap = {
            "/sys/class/block/mmcblk0p1/dev": "179:1",
            "/sys/class/block/mmcblk0p2/dev": "179:2",
            "/sys/class/block/mmcblk1p1/dev": "179:9",
        }
        content = devmap.get(path, "0:0")
        m = mock.mock_open(read_data=content)
        return m()

    with (
        mock.patch("station_agent.storage.os.stat", return_value=fake_stat),
        mock.patch("station_agent.storage.os.listdir", side_effect=fake_listdir),
        mock.patch("station_agent.storage.os.path.exists", side_effect=fake_exists),
        mock.patch("builtins.open", side_effect=fake_open),
    ):
        result = storage._root_block_device()

    # Should find mmcblk0p2 (major=179 minor=2) and strip 'p2' → 'mmcblk0'
    assert result == "mmcblk0"
