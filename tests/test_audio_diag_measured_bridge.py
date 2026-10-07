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


# ---------------------------------------------------------------------------
# Finding B — MeasuredTxBridge fd-ownership handoff (stop vs read_measurement)
# ---------------------------------------------------------------------------


def _make_bridge_with_open_pipe():
    """Return a started MeasuredTxBridge whose read_fd is a real OS pipe."""
    r, w = os.pipe()
    os.write(w, b"\x01\x00" * 80)  # 80 S16LE samples = 160 bytes
    os.close(w)

    def fake_spawn(make_argv):
        return _FakeProc(), r

    br = d.MeasuredTxBridge("tx.node", 47060, 16000, spawn=fake_spawn, socket_factory=_FakeSock)
    br.start()
    return br


def test_stop_before_read_measurement_returns_empty():
    """stop() takes the fd; subsequent read_measurement sees None and returns b''."""
    br = _make_bridge_with_open_pipe()
    br.stop()
    # read_measurement after stop → fd already taken by stop → returns b""
    result = br.read_measurement(160, timeout=1.0)
    assert result == b"", f"Expected b'' after stop, got {result!r}"


def test_read_measurement_then_stop_no_double_close():
    """read_measurement takes the fd; subsequent stop() sees None and does not double-close."""
    br = _make_bridge_with_open_pipe()
    data = br.read_measurement(160, timeout=1.0)
    assert len(data) == 160
    # stop() after read_measurement → fd already taken → must not raise
    br.stop()  # should be a no-op on the fd, no OSError


def test_read_measfd_is_sole_closer_of_fd():
    """read_measurement must rely on the injected read_measfd to close the fd — and NOT
    close it a second time itself. A following stop() must not touch that fd either.

    Regression guard for the double-close race: a `finally: os.close(fd)` in
    read_measurement would close the fd twice, and under concurrency the second
    close could hit a reused (wrong) descriptor.
    """
    r, w = os.pipe()
    os.write(w, b"\x02\x00" * 80)  # 160 bytes
    os.close(w)

    closes: list[int] = []

    def tracking_read_measfd(read_fd, nbytes, timeout):
        """Mimic the real contract: read then close the fd once; record each close."""
        buf = os.read(read_fd, nbytes)
        os.close(read_fd)
        closes.append(read_fd)
        return buf

    def fake_spawn(make_argv):
        return _FakeProc(), r

    br = d.MeasuredTxBridge(
        "tx.node",
        47061,
        16000,
        spawn=fake_spawn,
        read_measfd=tracking_read_measfd,
        socket_factory=_FakeSock,
    )
    br.start()

    data = br.read_measurement(160, timeout=1.0)
    assert data == b"\x02\x00" * 80
    # The injected reader closed the fd exactly once; read_measurement did not add a close.
    assert closes == [r], f"Expected read_measfd to be the sole closer, got closes={closes}"
    # Ownership was handed off — the bridge no longer holds the fd.
    assert br._read_fd is None
    # A following stop() must NOT attempt to close that same fd again.
    br.stop()
    assert closes == [r], f"stop() must not re-close the fd, got closes={closes}"


def test_measured_tx_argv_with_dsp_measures_post_limiter():
    from station_agent.audio import tx_dsp
    from station_agent.audio.diagnostics import build_measured_tx_argv

    argv = build_measured_tx_argv("n", 47000, 16000, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0))
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert len(dyn) == 3 and dyn[-1] < argv.index("tee")
    assert argv.index("audio/x-raw,format=F32LE,rate=16000,channels=1") < argv.index(
        "audiocheblimit"
    )
    # the limiter's last argument is immediately followed by the tee (limiter is last stage)
    assert argv[dyn[-1] + 5 : dyn[-1] + 7] == ["!", "tee"]
    assert argv[dyn[-1] + 4].startswith("threshold=")


def test_measured_tx_argv_without_dsp_unchanged():
    from station_agent.audio.diagnostics import build_measured_tx_argv

    assert "audiodynamic" not in build_measured_tx_argv("n", 47000, 16000)


def _started_argv(dsp, probe):
    from station_agent.audio import diagnostics as d

    seen = {}

    def fake_spawn(make_argv):
        seen["argv"] = make_argv(9)
        return object(), None

    br = d.MeasuredTxBridge(
        "tx.node",
        47070,
        16000,
        spawn=fake_spawn,
        socket_factory=_FakeSock,
        dsp=dsp,
        dsp_probe=probe,
    )
    return br, seen


def test_measured_bridge_probe_runs_in_start_not_construction():
    from station_agent.audio import tx_dsp

    calls = []
    br, seen = _started_argv(
        tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), lambda: calls.append(1) or True
    )
    assert calls == []  # nothing probed at construction (WS loop)
    br.start()
    assert calls == [1]
    assert "audiodynamic" in seen["argv"]


def test_measured_bridge_degrades_to_plain_when_dsp_unavailable():
    from station_agent.audio import tx_dsp

    br, seen = _started_argv(tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), lambda: False)
    br.start()
    assert "audiodynamic" not in seen["argv"]
    # measures what TxBridge transmits when degraded: the degraded-gain volume stage
    gain = tx_dsp.TxDspPolicy().degraded_gain
    assert f"volume={gain}" in seen["argv"]
    assert seen["argv"].index(f"volume={gain}") < seen["argv"].index("tee")


def test_make_diag_u_runs_no_probe():
    from station_agent.audio.bridge_factory import BridgeFactory

    calls = []
    f = BridgeFactory(dsp_probe=lambda: calls.append(1) or True)
    br = f.make_diag_u("tx.node", 16000)
    assert calls == []
    br.stop()
