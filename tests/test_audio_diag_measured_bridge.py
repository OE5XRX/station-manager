# tests/test_audio_diag_measured_bridge.py
import os

import station_agent.audio.diagnostics as d


class _FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        pass


class _FakeProc:
    def __init__(self):
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def test_measured_bridge_spawns_measured_tx_argv_and_reads_pipe():
    captured = {}

    def fake_spawn(make_argv):
        r, w = os.pipe()
        captured["argv"] = make_argv(w)
        os.write(w, b"\x01\x00" * 160)  # 160 S16LE samples
        os.close(w)
        return _FakeProc(), r

    br = d.MeasuredTxBridge("tx.node", 47050, 16000, spawn=fake_spawn, socket_factory=_FakeSock)
    br.start()
    assert "gst-launch-1.0" in captured["argv"] and "udpsrc" in captured["argv"]
    assert "pipewiresink" in captured["argv"] and "fdsink" in captured["argv"]
    pcm = br.read_measurement(320, timeout=1.0)
    assert len(pcm) == 320
    br.stop()


def test_measured_bridge_feed_opus_wraps_rtp_to_port():
    sock = _FakeSock()

    def fake_spawn(make_argv):
        r, _w = os.pipe()
        os.close(_w)
        return _FakeProc(), r

    br = d.MeasuredTxBridge("tx.node", 47051, 16000, spawn=fake_spawn, socket_factory=lambda: sock)
    br.start()
    br.feed_opus(b"\xfc\xff\xfe")
    assert len(sock.sent) == 1
    datagram, addr = sock.sent[0]
    assert addr == ("127.0.0.1", 47051)
    assert len(datagram) > 3  # RTP header prepended
    br.stop()


def test_measured_bridge_feed_before_start_is_safe():
    br = d.MeasuredTxBridge("tx.node", 47052, 16000, socket_factory=_FakeSock)
    br.feed_opus(b"\x00")  # must not raise (no socket yet)
