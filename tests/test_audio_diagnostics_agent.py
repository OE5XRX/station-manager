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


def test_run_diagnostic_anchor_u_fails_fast():
    """anchor U must return an error dict immediately without spawning anything."""

    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"

        def tx_sink_node(self, slot):
            return "FM.Mono"

        def get_volume(self, node):
            return 0.40

    spawn_called = []

    def fake_spawn(make_argv):
        spawn_called.append(True)
        return object(), None

    def fake_read(read_fd, nbytes, timeout):
        return b""

    rep = d.run_diagnostic(
        anchor="U",
        slot=1,
        signal={"level_dbfs": -20.0},
        backend=FakeBackend(),
        spawn=fake_spawn,
        read_measfd=fake_read,
    )
    assert "error" in rep, "anchor U must return an error dict"
    assert "follow-up" in rep["error"], f"error should mention 'follow-up', got: {rep['error']!r}"
    assert not spawn_called, "spawn must NOT be called for anchor U"


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
    # Floored at REF_SETTLE_MS + REF_WINDOW_MS → nbytes must be >= settle+window floor
    min_nbytes = int(16000 * (d.REF_SETTLE_MS + d.REF_WINDOW_MS) / 1000) * 2
    assert captured["nbytes"] >= min_nbytes, (
        f"nbytes={captured['nbytes']} is below the settle+window floor ({min_nbytes})"
    )


# ---------------------------------------------------------------------------
# FIX 1: _default_spawn fd leak — both r and w must be closed on failure
# ---------------------------------------------------------------------------


def test_default_spawn_closes_both_fds_on_popen_failure():
    """On Popen failure both r and w must be closed; no fd leak."""
    import os

    import pytest

    orig_pipe = d._os.pipe

    saved_fds = {}

    def capturing_pipe():
        r, w = orig_pipe()
        saved_fds["r"] = r
        saved_fds["w"] = w
        return r, w

    d._os.pipe = capturing_pipe
    orig_popen = d._subprocess.Popen

    def boom(*args, **kwargs):
        raise OSError("fake Popen failure")

    d._subprocess.Popen = boom
    try:
        with pytest.raises(OSError, match="fake Popen failure"):
            d._default_spawn(lambda fd: ["false", str(fd)])
    finally:
        d._os.pipe = orig_pipe
        d._subprocess.Popen = orig_popen

    # Both fds must be closed — os.fstat on a closed fd raises OSError.
    for name, fd in saved_fds.items():
        try:
            os.fstat(fd)
            # If we reach here the fd is still open → close it (prevents fd pollution)
            # and then fail the assertion.
            os.close(fd)
            raise AssertionError(f"fd {name}={fd} was NOT closed after Popen failure")
        except OSError:
            pass  # expected: fd is closed


def test_default_spawn_closes_both_fds_on_make_argv_failure():
    """On make_argv failure (before Popen) both r and w must be closed."""
    import os

    import pytest

    orig_pipe = d._os.pipe
    saved_fds = {}

    def capturing_pipe():
        r, w = orig_pipe()
        saved_fds["r"] = r
        saved_fds["w"] = w
        return r, w

    d._os.pipe = capturing_pipe
    try:
        with pytest.raises(ValueError, match="intentional"):
            d._default_spawn(lambda fd: (_ for _ in ()).throw(ValueError("intentional")))
    finally:
        d._os.pipe = orig_pipe

    for name, fd in saved_fds.items():
        try:
            os.fstat(fd)
            os.close(fd)
            raise AssertionError(f"fd {name}={fd} was NOT closed after make_argv failure")
        except OSError:
            pass  # expected: fd is closed


# ---------------------------------------------------------------------------
# FIX 2: window clamp floor raised to REF_SETTLE_MS + REF_WINDOW_MS (500 ms)
# ---------------------------------------------------------------------------


def test_duration_clamp_floor_is_settle_plus_window():
    """Requested 100 ms or negative → clamped to REF_SETTLE_MS + REF_WINDOW_MS = 500 ms."""

    class FakeBackend:
        def resolve_node(self, s, d_):
            return "n"

        def tx_sink_node(self, s):
            return None

        def get_volume(self, n):
            return None

    for dur_ms_req in (100, -1, 0):
        captured = {}

        def fake_read(read_fd, nbytes, timeout, _cap=captured):
            _cap["nbytes"] = nbytes
            return b""

        d.run_diagnostic(
            anchor="C",
            slot=1,
            signal={"kind": "sine", "level_dbfs": -20.0, "duration_ms": dur_ms_req},
            backend=FakeBackend(),
            spawn=lambda make_argv: (object(), None),
            read_measfd=fake_read,
        )
        floor_ms = d.REF_SETTLE_MS + d.REF_WINDOW_MS  # 500
        expected_min_nbytes = int(16000 * floor_ms / 1000) * 2
        assert captured["nbytes"] >= expected_min_nbytes, (
            f"dur_ms_req={dur_ms_req}: nbytes={captured['nbytes']} < floor {expected_min_nbytes}"
        )


def test_duration_clamp_huge_caps_at_max():
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
        signal={"kind": "sine", "level_dbfs": -20.0, "duration_ms": 10_000_000},
        backend=FakeBackend(),
        spawn=lambda make_argv: (object(), None),
        read_measfd=fake_read,
    )
    max_nbytes = int(16000 * d.MAX_DURATION_MS / 1000) * 2
    assert captured["nbytes"] <= max_nbytes


# ---------------------------------------------------------------------------
# FIX 3: D tap — unavailable vs muted distinction
# ---------------------------------------------------------------------------


def test_d_tap_unavailable_when_sink_volume_none():
    """When get_volume returns None → D silent=False, rms=None, note mentions unavailable."""

    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"

        def tx_sink_node(self, slot):
            return "FM.Mono"

        def get_volume(self, node):
            return None  # wpctl failed

    ref_pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)

    rep = d.run_diagnostic(
        anchor="C",
        slot=1,
        signal={"level_dbfs": -20.0, "duration_ms": 500},
        backend=FakeBackend(),
        spawn=lambda make_argv: (object(), None),
        read_measfd=lambda fd, nb, to: ref_pcm,
    )
    taps = {t["point"]: t for t in rep["taps"]}
    d_tap = taps["D"]
    assert d_tap["silent"] is False, (
        "D should NOT be marked silent when sink volume is unavailable"
    )
    assert d_tap["rms_dbfs"] is None
    note = d_tap.get("note", "")
    assert "unavailable" in note.lower(), f"note should mention 'unavailable', got: {note!r}"


def test_d_tap_silent_when_sink_muted():
    """When get_volume returns 0.0 (muted) → D silent=True, note mentions muted."""

    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"

        def tx_sink_node(self, slot):
            return "FM.Mono"

        def get_volume(self, node):
            return 0.0  # muted

    ref_pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)

    rep = d.run_diagnostic(
        anchor="C",
        slot=1,
        signal={"level_dbfs": -20.0, "duration_ms": 500},
        backend=FakeBackend(),
        spawn=lambda make_argv: (object(), None),
        read_measfd=lambda fd, nb, to: ref_pcm,
    )
    taps = {t["point"]: t for t in rep["taps"]}
    d_tap = taps["D"]
    assert d_tap["silent"] is True, "D should be silent when sink is muted"
    assert d_tap["rms_dbfs"] is None
    note = d_tap.get("note", "")
    assert "muted" in note.lower(), f"note should mention 'muted', got: {note!r}"
