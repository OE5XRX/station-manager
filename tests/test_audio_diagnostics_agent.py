import math

from station_agent.audio import diagnostics as d


def test_parse_wpctl_volume():
    assert d.parse_wpctl_volume("Volume: 0.40") == 0.40
    assert d.parse_wpctl_volume("Volume: 1.00 [MUTED]") == 1.00
    assert d.parse_wpctl_volume("garbage") is None


def test_collect_static_gains_reports_sink_db():
    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"
        def tx_sink_node(self, slot):
            return "FM.Mono"
        def get_volume(self, node):
            return 0.40
    g = d.collect_static_gains(FakeBackend(), 1)
    assert g["sink_volume_linear"] == 0.40
    assert abs(g["sink_volume_db"] - (20 * math.log10(0.40))) < 0.01


def test_collect_static_gains_tolerates_missing_volume():
    class FakeBackend:
        def resolve_node(self, slot, direction): return "n"
        def tx_sink_node(self, slot): return None
        def get_volume(self, node): return None
    g = d.collect_static_gains(FakeBackend(), 1)
    assert g["sink_volume_linear"] is None and g["sink_volume_db"] is None


def test_measured_inject_has_tee_sink_and_measfd():
    argv = d.build_measured_inject_argv("oe5xrx.slot1.tx", 1000, -20.0, 16000)
    s = " ".join(argv)
    assert "audiotestsrc" in s and "wave=sine" in s and "freq=1000" in s
    assert "opusenc" in s and "opusdec" in s        # real roundtrip, mirrors mic path
    assert "tee" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert "sync=false" in s                          # playback sink un-clocked
    assert f"fd={d.MEAS_FD}" in s                     # synchronous measurement branch
    assert "is-live=true" in s


def test_measured_tx_binds_loopback_and_taps():
    argv = d.build_measured_tx_argv("oe5xrx.slot1.tx", 47000, 16000)
    s = " ".join(argv)
    assert "udpsrc" in s and "address=127.0.0.1" in s and "port=47000" in s
    assert "rtpjitterbuffer" in s and "opusdec" in s
    assert "tee" in s and f"fd={d.MEAS_FD}" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert "sync=false" in s                          # playback sink un-clocked


def test_reverse_tap_delegates_to_selftest():
    argv = d.build_reverse_tap_argv("hw:1,0,0", 8000, 1.0)
    assert isinstance(argv, list)
    assert argv[0] == "arecord"                       # delegates to selftest.build_tx_capture_argv
