import io
import struct
import subprocess

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
    opus_bridge.probe_dsp_available.cache_clear()
    assert opus_bridge.probe_dsp_available(inspect=lambda n: True) is True
    opus_bridge.probe_dsp_available.cache_clear()
    assert opus_bridge.probe_dsp_available(inspect=lambda n: n != "audiodynamic") is False
    opus_bridge.probe_dsp_available.cache_clear()

    def raising(n):
        raise OSError("no gst-inspect")

    assert opus_bridge.probe_dsp_available(inspect=raising) is False
    opus_bridge.probe_dsp_available.cache_clear()


def test_factory_make_tx_uses_current_ceiling_and_probe():
    s = TxAudioSettings()
    s.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -8.0}})
    f = BridgeFactory(port_base=47100, tx_settings=s, dsp_probe=lambda: False)
    b = f.make_tx("n", 16000, on_meter=lambda r: None)
    assert b._dsp.ceiling_dbfs == -8.0 and b._dsp.enabled is False
    b2 = BridgeFactory(port_base=47200).make_tx("n", 16000)  # no settings -> safe default
    assert b2._dsp.ceiling_dbfs == -12.0
