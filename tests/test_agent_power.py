from unittest import mock

from station_agent import power


def test_parse_throttled_undervoltage_now_and_occurred():
    # bit0 (now) + bit16 (occurred)
    result = power.parse_throttled(0x10001)
    assert result["undervoltage_now"] is True
    assert result["undervoltage_occurred"] is True
    assert result["throttled_now"] is False


def test_parse_throttled_throttled_occurred_only():
    result = power.parse_throttled(0x40000)  # bit18
    assert result["throttled_occurred"] is True
    assert result["throttled_now"] is False


def test_parse_throttled_clean():
    result = power.parse_throttled(0x0)
    assert not any(result.values())


def test_read_throttle_absent_binary_returns_none():
    with mock.patch("shutil.which", return_value=None):
        assert power.read_throttle() is None


def test_read_throttle_parses_vcgencmd_output():
    with mock.patch("shutil.which", return_value="/usr/bin/vcgencmd"), \
         mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="throttled=0x50005\n")
        result = power.read_throttle()
    assert result["throttled_hex"] == "0x50005"
    assert result["undervoltage_now"] is True       # 0x5...=bit0
    assert result["undervoltage_occurred"] is True  # 0x5....=bit16
