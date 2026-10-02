import math

from station_agent.audio import diagnostics as d

# ---------------------------------------------------------------------------
# parse_wpctl_volume
# ---------------------------------------------------------------------------


def test_parse_wpctl_volume():
    assert d.parse_wpctl_volume("Volume: 0.40") == 0.40
    # BUG3 fix: [MUTED] → 0.0 (muted sink = effectively zero gain)
    assert d.parse_wpctl_volume("Volume: 1.00 [MUTED]") == 0.0
    assert d.parse_wpctl_volume("garbage") is None


# ---------------------------------------------------------------------------
# collect_static_gains
# ---------------------------------------------------------------------------


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
        def resolve_node(self, slot, direction):
            return "n"

        def tx_sink_node(self, slot):
            return None

        def get_volume(self, node):
            return None

    g = d.collect_static_gains(FakeBackend(), 1)
    assert g["sink_volume_linear"] is None and g["sink_volume_db"] is None


# ---------------------------------------------------------------------------
# argv builders
# ---------------------------------------------------------------------------


def test_measured_inject_has_tee_sink_and_measfd():
    argv = d.build_measured_inject_argv("oe5xrx.slot1.tx", 1000, -20.0, 16000)
    s = " ".join(argv)
    assert "audiotestsrc" in s and "wave=sine" in s and "freq=1000" in s
    assert "opusenc" in s and "opusdec" in s  # real roundtrip, mirrors mic path
    assert "tee" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert "sync=false" in s  # playback sink un-clocked
    # BUG1: default call still uses MEAS_FD constant (for backward compat of test)
    assert f"fd={d.MEAS_FD}" in s
    assert "is-live=true" in s
    # BUG2: audiotestsrc must carry volume=1.0 so only the volume element sets the level
    assert "volume=1.0" in s


def test_measured_inject_fd_is_parameterized():
    """BUG1: meas_fd is dynamic; passing a non-default fd must appear in the argv."""
    argv = d.build_measured_inject_argv("oe5xrx.slot1.tx", 1000, -20.0, 16000, meas_fd=7)
    s = " ".join(argv)
    assert "fd=7" in s
    # The hardcoded fallback (fd=3) must NOT appear in the fdsink element when meas_fd=7
    # (volume=1.0 on audiotestsrc contains no "fd=3", and fdsink now says fd=7).
    fdsink_idx = argv.index("fdsink")
    assert argv[fdsink_idx + 1] == "fd=7"


def test_measured_tx_fd_is_parameterized():
    """BUG1: same fd-parameterisation check for the TX tap builder."""
    argv = d.build_measured_tx_argv("oe5xrx.slot1.tx", 47000, 16000, meas_fd=9)
    fdsink_idx = argv.index("fdsink")
    assert argv[fdsink_idx + 1] == "fd=9"


def test_measured_tx_binds_loopback_and_taps():
    argv = d.build_measured_tx_argv("oe5xrx.slot1.tx", 47000, 16000)
    s = " ".join(argv)
    assert "udpsrc" in s and "address=127.0.0.1" in s and "port=47000" in s
    assert "rtpjitterbuffer" in s and "opusdec" in s
    assert "tee" in s and f"fd={d.MEAS_FD}" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert "sync=false" in s  # playback sink un-clocked


def test_reverse_tap_delegates_to_selftest():
    argv = d.build_reverse_tap_argv("hw:1,0,0", 8000, 1.0)
    assert isinstance(argv, list)
    assert argv[0] == "arecord"  # delegates to selftest.build_tx_capture_argv


# ---------------------------------------------------------------------------
# run_diagnostic — seam signatures updated for BUG1
#
# spawn(make_argv) -> (proc, read_fd)
# read_measfd(read_fd, nbytes, timeout) -> bytes
# ---------------------------------------------------------------------------


def test_run_diagnostic_anchor_c_reports_c_and_derived_d():
    ref_pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)

    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"

        def tx_sink_node(self, slot):
            return "FM.Mono"

        def get_volume(self, node):
            return 0.40

    spawned = {}

    def fake_spawn(make_argv):
        # Call make_argv with a non-default fd to exercise the parameterisation
        argv = make_argv(7)
        spawned["argv"] = argv
        spawned["fd_in_argv"] = "fd=7" in " ".join(argv)
        return object(), None  # (proc, read_fd)

    def fake_read(read_fd, nbytes, timeout):
        return ref_pcm

    rep = d.run_diagnostic(
        anchor="C",
        slot=1,
        signal={"kind": "sine", "freq_hz": 1000, "level_dbfs": -20.0, "duration_ms": 500},
        backend=FakeBackend(),
        spawn=fake_spawn,
        read_measfd=fake_read,
    )
    assert rep["anchor"] == "C"
    # Verify the make_argv factory baked the fd into the argv
    assert spawned.get("fd_in_argv"), "make_argv did not embed the fd passed to it"
    taps = {t["point"]: t for t in rep["taps"]}
    assert abs(taps["C"]["peak_dbfs"] - (-20.0)) < 0.5
    assert taps["C"]["computed"] is False
    # D = C + 20log10(0.40) ~= C - 7.96 dB, flagged computed
    assert taps["D"]["computed"] is True
    assert abs(taps["D"]["rms_dbfs"] - (taps["C"]["rms_dbfs"] - 7.96)) < 0.1
    assert rep["static_gains"]["sink_volume_linear"] == 0.40


def test_run_diagnostic_clamps_absurd_duration():
    class FakeBackend:
        def resolve_node(self, s, d_):
            return "n"

        def tx_sink_node(self, s):
            return None

        def get_volume(self, n):
            return None

    captured = {}

    def fake_read(read_fd, nbytes, timeout):
        captured["timeout"] = timeout
        captured["nbytes"] = nbytes
        return b"\x00\x00" * 10

    d.run_diagnostic(
        anchor="C",
        slot=1,
        signal={"kind": "sine", "level_dbfs": -20.0, "duration_ms": 10_000_000},
        backend=FakeBackend(),
        spawn=lambda make_argv: (object(), None),
        read_measfd=fake_read,
    )
    assert captured["timeout"] <= (d.MAX_DURATION_MS / 1000) + 1.0


def test_run_diagnostic_floors_negative_duration():
    """BUG5: negative duration_ms must not produce negative/zero nbytes."""

    class FakeBackend:
        def resolve_node(self, s, d_):
            return "n"

        def tx_sink_node(self, s):
            return None

        def get_volume(self, n):
            return None

    captured = {}

    def fake_read(read_fd, nbytes, timeout):
        captured["nbytes"] = nbytes
        return b""

    d.run_diagnostic(
        anchor="C",
        slot=1,
        signal={"kind": "sine", "level_dbfs": -20.0, "duration_ms": -999},
        backend=FakeBackend(),
        spawn=lambda make_argv: (object(), None),
        read_measfd=fake_read,
    )
    # Floored at REF_WINDOW_MS → nbytes must be positive
    min_nbytes = int(16000 * d.REF_WINDOW_MS / 1000) * 2
    assert captured["nbytes"] >= min_nbytes, (
        f"nbytes={captured['nbytes']} is below the REF_WINDOW_MS floor ({min_nbytes})"
    )
