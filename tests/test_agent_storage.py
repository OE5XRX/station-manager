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
    result = storage.parse_extcsd(
        "eMMC Pre EOL information [EXT_CSD_PRE_EOL_INFO]: 0x03\n"
    )
    assert result["pre_eol"] == "urgent"


def test_count_mmc_io_errors():
    assert storage.count_mmc_io_errors(DMESG_SAMPLE) == 2


def test_count_mmc_io_errors_none():
    assert storage.count_mmc_io_errors("[0.0] clean boot\n") == 0
