import io
import struct
import subprocess
import threading
import time

from station_agent.audio import opus_bridge, tx_dsp
from station_agent.audio.bridge_factory import BridgeFactory
from station_agent.audio.tx_settings import TxAudioSettings


class FakeProc:
    def __init__(self, stdout=None, exits_early=False):
        self.stdout = stdout
        self._early = exits_early
        self.terminated = False

    def poll(self):
        return 1 if (self._early or self.terminated) else None

    def wait(self, timeout=None):
        if self._early or self.terminated:
            return 1
        raise subprocess.TimeoutExpired("gst", timeout)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class FakeSock:
    def sendto(self, *a):
        pass

    def close(self):
        pass


def _spawner(procs):
    calls = []

    def spawn(argv):
        calls.append(argv)
        return procs.pop(0)

    return spawn, calls


def _bridge(spawn, **kw):
    kw.setdefault("dsp_probe", lambda: True)
    return opus_bridge.TxBridge(
        "n", 47000, 16000, spawn=spawn, socket_factory=FakeSock, startup_grace=0.01, **kw
    )


def test_no_dsp_no_meter_is_legacy_argv():
    spawn, calls = _spawner([FakeProc()])
    b = _bridge(spawn)
    b.start()
    assert calls[0] == opus_bridge.build_tx_argv("n", 47000, 16000)
    assert b.dsp_mode == "off"
    b.stop()


def test_full_dsp_spawned_and_mode_full():
    spawn, calls = _spawner([FakeProc()])
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0))
    b.start()
    assert "audiodynamic" in calls[0]
    assert b.dsp_mode == "full"
    b.stop()


def test_dsp_pipeline_dying_at_startup_falls_back_to_degraded():
    spawn, calls = _spawner([FakeProc(exits_early=True), FakeProc()])
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0))
    b.start()
    assert len(calls) == 2
    assert "audiodynamic" not in calls[1] and "volume=1.0" in calls[1]
    assert b.dsp_mode == "degraded"
    b.stop()


def test_disabled_config_is_degraded_without_retry():
    spawn, calls = _spawner([FakeProc()])
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0, enabled=False))
    b.start()
    assert len(calls) == 1 and b.dsp_mode == "degraded"
    b.stop()


def test_meter_reader_emits_readings_from_stdout():
    chunk = struct.pack("<2000f", *([1.0, -1.0] * 1000))
    proc = FakeProc(stdout=io.BytesIO(chunk * 2))
    spawn, _ = _spawner([proc])
    got = []
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=got.append)
    b.start()
    b.stop()  # joins the reader; BytesIO EOF ends it
    assert len(got) == 2
    assert got[0]["limiting"] is True
    assert got[0]["dsp"] == "full" and got[0]["ceiling_dbfs"] == -12.0


def test_meter_callback_exception_does_not_kill_reader():
    chunk = struct.pack("<2000f", *([0.1] * 2000))
    proc = FakeProc(stdout=io.BytesIO(chunk * 3))
    spawn, _ = _spawner([proc])
    seen = []

    def boom(r):
        seen.append(r)
        raise RuntimeError("consumer bug")

    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=boom)
    b.start()
    b.stop()
    assert len(seen) == 3


def test_probe_dsp_available_uses_inspect_and_handles_oserror():
    assert opus_bridge.probe_dsp_available(inspect=lambda n: True) is True
    assert opus_bridge.probe_dsp_available(inspect=lambda n: n != "audiodynamic") is False

    def raising(n):
        raise OSError("no gst-inspect")

    assert opus_bridge.probe_dsp_available(inspect=raising) is False


def test_probe_timeout_is_not_cached_but_definitive_result_is(monkeypatch):
    opus_bridge._reset_probe_cache()
    calls = []

    def fake_exists(name):
        calls.append(name)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("gst-inspect-1.0", 5)
        return True

    monkeypatch.setattr(opus_bridge, "_gst_inspect_exists", fake_exists)
    assert opus_bridge.probe_dsp_available() is False  # timeout: False, not memoised
    assert opus_bridge.probe_dsp_available() is True  # re-probed
    n = len(calls)
    assert opus_bridge.probe_dsp_available() is True  # definitive: memoised
    assert len(calls) == n
    opus_bridge._reset_probe_cache()


def test_factory_make_tx_does_not_probe_and_uses_current_ceiling():
    probed = []
    s = TxAudioSettings()
    s.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -8.0}})
    f = BridgeFactory(port_base=47100, tx_settings=s, dsp_probe=lambda: probed.append(1) or False)
    b = f.make_tx("n", 16000, on_meter=lambda r: None)
    assert probed == []  # make_tx runs on the WS loop: no subprocess/probe
    assert b._dsp.ceiling_dbfs == -8.0
    b2 = BridgeFactory(port_base=47200).make_tx("n", 16000)  # no settings -> safe default
    assert b2._dsp.ceiling_dbfs == -12.0


def test_start_invokes_probe_and_degrades_when_unavailable():
    probed = []
    spawn, calls = _spawner([FakeProc()])
    b = _bridge(
        spawn,
        dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0),
        dsp_probe=lambda: probed.append(1) or False,
    )
    b.start()
    assert probed == [1]
    assert b.dsp_mode == "degraded" and "audiodynamic" not in calls[0]
    b.stop()


def test_fallback_with_meter_reader_runs_on_fallback_proc_degraded():
    chunk = struct.pack("<2000f", *([1.0, -1.0] * 1000))
    dead = FakeProc(stdout=io.BytesIO(b""), exits_early=True)
    fallback = FakeProc(stdout=io.BytesIO(chunk))
    spawn, calls = _spawner([dead, fallback])
    got = []
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=got.append)
    b.start()
    b.stop()
    assert dead.stdout.closed  # no fd leak
    assert len(got) == 1
    # threshold None (no limiter in the degraded path): clip modelled at full scale
    assert got[0]["dsp"] == "degraded" and got[0]["peak_dbfs"] == 0.0
    assert got[0]["limiting"] is False


def test_fallback_also_dying_is_failed_and_logged(caplog):
    spawn, calls = _spawner([FakeProc(exits_early=True), FakeProc(exits_early=True)])
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0))
    with caplog.at_level("ERROR"):
        b.start()
    assert len(calls) == 2  # no further retry
    assert b.dsp_mode == "failed"
    assert "failed even without DSP" in caplog.text
    b.stop()


class _ChunkyStream:
    """Returns at most 1-3 bytes per read."""

    def __init__(self, data):
        self._data = data
        self._i = 0
        self._step = 0

    def read(self, n):
        k = min(n, 1 + self._step % 3, len(self._data) - self._i)
        self._step += 1
        out = self._data[self._i : self._i + k]
        self._i += k
        return out


def test_read_exact_stitches_short_reads_and_drops_short_tail():
    stream = _ChunkyStream(bytes(range(10)))
    assert opus_bridge._read_exact(stream, 4) == bytes([0, 1, 2, 3])
    assert opus_bridge._read_exact(stream, 4) == bytes([4, 5, 6, 7])
    assert opus_bridge._read_exact(stream, 4) == b""  # only 2 bytes left at EOF


def test_stop_is_idempotent_and_safe_after_failed_start():
    b = _bridge(lambda argv: FakeProc())
    b.stop()  # never started
    b.stop()

    def boom(argv):
        raise OSError("gst-launch missing")

    b2 = _bridge(boom)
    try:
        b2.start()
    except OSError:
        pass
    b2.stop()
    b2.stop()

    spawn, _ = _spawner([FakeProc(stdout=io.BytesIO(b""))])
    b3 = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=lambda r: None)
    b3.start()
    b3.stop()
    b3.stop()


def test_stop_returns_within_bound_when_reader_is_slow(monkeypatch):
    release = threading.Event()

    class Blocking:
        closed = False

        def read(self, n):
            release.wait(5)
            return b""

        def close(self):
            self.closed = True

    monkeypatch.setattr(opus_bridge, "_STOP_WAIT", 0.2)
    spawn, _ = _spawner([FakeProc(stdout=Blocking())])
    b = _bridge(spawn, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=lambda r: None)
    b.start()
    t0 = time.monotonic()
    b.stop()
    assert time.monotonic() - t0 < 2.0
    release.set()
