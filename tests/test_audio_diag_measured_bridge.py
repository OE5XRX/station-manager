# tests/test_audio_diag_measured_bridge.py
import math
import os
import struct

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


class _FakeBackend:
    def tx_sink_node(self, slot):
        return "sink.node"

    def get_volume(self, node):
        return 0.40


def _sine_pcm(level_dbfs, n=16000, rate=16000, freq=1000):
    amp = (10 ** (level_dbfs / 20)) * 32767
    return struct.pack(
        f"<{n}h",
        *[
            max(-32768, min(32767, int(round(amp * math.sin(2 * math.pi * freq * i / rate)))))
            for i in range(n)
        ],
    )


class _CannedBridge:
    def __init__(self, pcm):
        self._pcm = pcm

    def read_measurement(self, nbytes, timeout):
        return self._pcm[:nbytes]


def test_finish_u_builds_report_with_c_measured_and_d_projected():
    pcm = _sine_pcm(-20.0)
    rep = d.finish_u_diagnostic(
        _CannedBridge(pcm),
        backend=_FakeBackend(),
        slot=0,
        signal={"freq_hz": 1000, "level_dbfs": -20.0},
        rate=16000,
    )
    assert rep["anchor"] == "U"
    pts = {t["point"]: t for t in rep["taps"]}
    assert pts["C"]["computed"] is False and pts["C"]["silent"] is False
    assert pts["C"]["peak_dbfs"] == -20.0 or abs(pts["C"]["peak_dbfs"] + 20.0) < 0.2
    assert pts["D"]["computed"] is True
    assert abs(pts["D"]["rms_dbfs"] - (pts["C"]["rms_dbfs"] - 7.96)) < 0.05
    assert rep["static_gains"]["sink_volume_db"] == -7.96


def test_finish_u_silent_capture_reports_c_silent():
    rep = d.finish_u_diagnostic(
        _CannedBridge(b""),
        backend=_FakeBackend(),
        slot=0,
        signal={},
        rate=16000,
    )
    pts = {t["point"]: t for t in rep["taps"]}
    assert pts["C"]["silent"] is True and pts["D"]["silent"] is True


def test_finish_u_reference_uses_fixture_constants_not_client_signal():
    """Fix 2: anchor U reports the FIXED fixture constants, not the client-supplied signal.

    Even when the client asks for freq=440/level=-6/duration=5000, the reported
    reference must reflect the committed fixture (1 kHz / -20 dBFS / 300 ms window).
    """
    pcm = _sine_pcm(-20.0)
    rep = d.finish_u_diagnostic(
        _CannedBridge(pcm),
        backend=_FakeBackend(),
        slot=0,
        # Client requests non-default values — these must NOT appear in reference.
        signal={"freq_hz": 440, "level_dbfs": -6.0, "duration_ms": 5000},
        rate=16000,
    )
    assert rep["reference"] == {
        "freq_hz": d.REF_FREQ_HZ,
        "level_dbfs": d.REF_LEVEL_DBFS,
        "window_ms": d.REF_WINDOW_MS,
    }, f"Expected fixture constants in reference, got: {rep['reference']}"
