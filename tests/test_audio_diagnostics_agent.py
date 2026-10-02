from station_agent.audio import diagnostics as d


def test_measured_inject_has_tee_sink_and_measfd():
    argv = d.build_measured_inject_argv("oe5xrx.slot1.tx", 1000, -20.0, 16000)
    s = " ".join(argv)
    assert "audiotestsrc" in s and "wave=sine" in s and "freq=1000" in s
    assert "opusenc" in s and "opusdec" in s        # real roundtrip, mirrors mic path
    assert "tee" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert f"fd={d.MEAS_FD}" in s                     # synchronous measurement branch
    assert "is-live=true" in s


def test_measured_tx_binds_loopback_and_taps():
    argv = d.build_measured_tx_argv("oe5xrx.slot1.tx", 47000, 16000)
    s = " ".join(argv)
    assert "udpsrc" in s and "address=127.0.0.1" in s and "port=47000" in s
    assert "rtpjitterbuffer" in s and "opusdec" in s
    assert "tee" in s and f"fd={d.MEAS_FD}" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
