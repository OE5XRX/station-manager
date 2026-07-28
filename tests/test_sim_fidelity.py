import termios

from tests.fake_fw import FakeFirmware


def test_sim_pty_is_raw_mode():
    fw = FakeFirmware({"fm1": {"identity": {}, "capabilities": []}})
    fw.start()
    try:
        attrs = termios.tcgetattr(fw._slave_fd)
        _iflag, oflag, _cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
        # Raw: canonical mode + echo OFF, output post-processing OFF.
        assert not (lflag & termios.ICANON), "PTY still in canonical mode — sim lies vs real UART"
        assert not (lflag & termios.ECHO), "PTY still echoes — sim lies vs real UART"
        assert not (oflag & termios.OPOST), "PTY still post-processes output"
    finally:
        fw.stop()
