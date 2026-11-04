"""Tests for station_agent.boot_markers (slot-boot-stage2 spec §5)."""

import pytest

from station_agent import boot_markers as bm

BASE = "console=ttyS0 root=PARTLABEL=root_a rootwait"


class TestParse:
    def test_no_markers_is_legacy_boot_path(self):
        m = bm.parse_boot_markers(BASE)
        assert m.stage2 is None
        assert m.overlays is None
        assert m.legacy
        assert m.commit_blockers() == []

    def test_all_ok(self):
        m = bm.parse_boot_markers(f"{BASE} oe5xrx.stage2=ok oe5xrx.overlays=ok:oe5xrx-ina226,foo")
        assert m.stage2 == "ok"
        assert m.overlays == "ok"
        assert m.overlay_names == ("oe5xrx-ina226", "foo")
        assert m.failed_overlays == ()
        assert not m.legacy
        assert m.commit_blockers() == []

    @pytest.mark.parametrize("value", ["ok", "ok:"])
    def test_overlays_ok_without_names(self, value):
        m = bm.parse_boot_markers(f"oe5xrx.stage2=ok oe5xrx.overlays={value}")
        assert m.overlays == "ok"
        assert m.overlay_names == ()
        assert m.commit_blockers() == []

    @pytest.mark.parametrize("value", ["missing", "failed"])
    def test_stage2_not_ok_blocks(self, value):
        m = bm.parse_boot_markers(f"{BASE} oe5xrx.stage2={value}")
        assert m.stage2 == value
        blockers = m.commit_blockers()
        assert len(blockers) == 1
        assert f"oe5xrx.stage2={value}" in blockers[0]

    def test_overlay_failed_blocks_and_names_overlay(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=ok oe5xrx.overlays=failed:oe5xrx-ina226")
        assert m.overlays == "failed"
        assert m.failed_overlays == ("oe5xrx-ina226",)
        blockers = m.commit_blockers()
        assert len(blockers) == 1
        assert "oe5xrx-ina226" in blockers[0]

    def test_overlays_marker_alone_without_stage2_still_gates(self):
        m = bm.parse_boot_markers("oe5xrx.overlays=failed:x")
        assert m.stage2 is None
        assert not m.legacy
        assert m.commit_blockers()

    def test_both_failing_reports_both(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=failed oe5xrx.overlays=failed:a")
        assert len(m.commit_blockers()) == 2

    @pytest.mark.parametrize("value", ["", "OK", "yes", "ok,extra", "ok:a"])
    def test_unknown_stage2_value_fails_closed(self, value):
        m = bm.parse_boot_markers(f"oe5xrx.stage2={value}")
        assert m.stage2 == value
        assert m.commit_blockers()

    def test_bare_stage2_flag_without_value_fails_closed(self):
        m = bm.parse_boot_markers("oe5xrx.stage2")
        assert m.stage2 == ""
        assert m.commit_blockers()

    @pytest.mark.parametrize("value", ["", "bogus", "failed", "failed:", "ok:a,,b", "partial:a"])
    def test_malformed_overlays_fails_closed(self, value):
        m = bm.parse_boot_markers(f"oe5xrx.stage2=ok oe5xrx.overlays={value}")
        assert m.overlays == "malformed"
        assert m.commit_blockers()

    def test_last_occurrence_wins(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=missing oe5xrx.stage2=ok")
        assert m.stage2 == "ok"
        m = bm.parse_boot_markers("oe5xrx.stage2=ok oe5xrx.stage2=failed")
        assert m.stage2 == "failed"

    def test_overlays_last_occurrence_wins(self):
        m = bm.parse_boot_markers("oe5xrx.overlays=failed:a oe5xrx.overlays=ok:a")
        assert m.overlays == "ok"
        assert m.commit_blockers() == []

    def test_init_side_marker_never_overrides_kernel_side(self):
        m = bm.parse_boot_markers("root=x oe5xrx.stage2=ok -- oe5xrx.stage2=failed")
        assert m.stage2 == "ok"
        m = bm.parse_boot_markers("root=x oe5xrx.stage2=failed -- oe5xrx.stage2=ok")
        assert m.stage2 == "failed"

    def test_marker_only_after_double_dash_still_gates(self):
        m = bm.parse_boot_markers("root=x -- oe5xrx.overlays=failed:a")
        assert not m.legacy
        assert m.commit_blockers()

    @pytest.mark.parametrize(
        ("cmdline", "stage2"),
        [
            ('dyndbg="a -- b" oe5xrx.stage2=failed', "failed"),
            ('"oe5xrx.stage2=failed"', "failed"),
            ('oe5xrx.stage2="failed"', "failed"),
            ('foo="x oe5xrx.stage2=ok" oe5xrx.stage2=failed', "failed"),
            ('oe5xrx.stage2=ok foo="oe5xrx.stage2=failed y"', "ok"),
        ],
        ids=[
            "quoted-dashes",
            "fully-quoted",
            "quoted-value",
            "marker-in-quoted-a",
            "marker-in-quoted-b",
        ],
    )
    def test_kernel_quote_handling(self, cmdline, stage2):
        assert bm.parse_boot_markers(cmdline).stage2 == stage2

    def test_quoted_double_dash_ends_kernel_args_like_the_kernel(self):
        # parse_args() compares the param after next_arg() stripped the quotes.
        m = bm.parse_boot_markers('oe5xrx.stage2=failed "--" oe5xrx.stage2=ok')
        assert m.stage2 == "failed"

    def test_trailing_quote_without_opening_quote_is_kept(self):
        m = bm.parse_boot_markers('oe5xrx.stage2=ok"')
        assert m.stage2 == 'ok"'
        assert m.commit_blockers()

    def test_only_kernel_whitespace_splits(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=failed\x1coe5xrx.stage2=ok")
        assert m.stage2 == "failed\x1coe5xrx.stage2=ok"
        assert m.commit_blockers()
        m = bm.parse_boot_markers("root=x\xa0oe5xrx.stage2=ok\x0boe5xrx.overlays=ok:a")
        assert m.stage2 == "ok"
        assert m.overlay_names == ("a",)

    def test_long_failed_overlay_list_is_clipped_in_blocker(self):
        names = ",".join(f"overlay{i:02d}-" + "x" * 50 for i in range(30))
        (blocker,) = bm.parse_boot_markers(f"oe5xrx.overlays=failed:{names}").commit_blockers()
        assert len(blocker) < 150
        assert "(30 overlays)" in blocker

    def test_unterminated_quote_swallows_rest(self):
        # Kernel semantics: an open quote runs to the end of the cmdline.
        m = bm.parse_boot_markers('oe5xrx.stage2=ok foo="x oe5xrx.stage2=failed')
        assert m.stage2 == "ok"

    def test_multiple_failed_overlays(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=ok oe5xrx.overlays=failed:a,b")
        assert m.failed_overlays == ("a", "b")
        assert "failed:a,b" in m.commit_blockers()[0]

    def test_similar_prefixes_are_ignored(self):
        m = bm.parse_boot_markers("xoe5xrx.stage2=failed oe5xrx.stage2x=failed oe5xrx.other=1")
        assert m.legacy

    def test_trailing_newline_and_tabs(self):
        m = bm.parse_boot_markers("root=x\toe5xrx.stage2=ok\toe5xrx.overlays=ok:a\n")
        assert m.stage2 == "ok"
        assert m.overlay_names == ("a",)

    def test_marker_text_is_sanitised_in_blocker(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=" + "\x1b" * 3 + "x" * 500)
        (blocker,) = m.commit_blockers()
        assert "\x1b" not in blocker
        assert len(blocker) < 200


class TestMetricValues:
    def test_legacy_reports_nothing(self):
        assert bm.parse_boot_markers(BASE).metric_values() == {}

    def test_ok(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=ok oe5xrx.overlays=ok:a,b")
        assert m.metric_values() == {"boot.stage2_ok": 1.0, "boot.overlays_failed": 0.0}

    def test_stage2_missing_without_overlays_marker(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=missing")
        assert m.metric_values() == {"boot.stage2_ok": 0.0}

    def test_overlay_failed(self):
        m = bm.parse_boot_markers("oe5xrx.stage2=ok oe5xrx.overlays=failed:a")
        assert m.metric_values() == {"boot.stage2_ok": 1.0, "boot.overlays_failed": 1.0}

    def test_multiple_failed_overlays_are_counted(self):
        m = bm.parse_boot_markers("oe5xrx.overlays=failed:a,b")
        assert m.metric_values() == {"boot.overlays_failed": 2.0}

    def test_overlays_malformed_counts_as_one_failure(self):
        m = bm.parse_boot_markers("oe5xrx.overlays=bogus")
        assert m.metric_values() == {"boot.overlays_failed": 1.0}


class TestRead:
    def test_reads_cmdline_file(self, tmp_path):
        p = tmp_path / "cmdline"
        p.write_text("root=x oe5xrx.stage2=failed\n")
        m = bm.read_boot_markers(str(p))
        assert m is not None and m.stage2 == "failed"

    def test_unreadable_returns_none(self, tmp_path):
        assert bm.read_boot_markers(str(tmp_path / "nope")) is None

    def test_undecodable_bytes_do_not_raise(self, tmp_path):
        p = tmp_path / "cmdline"
        p.write_bytes(b"root=\xff\xfe\xa0oe5xrx.stage2=ok\n")
        m = bm.read_boot_markers(str(p))
        assert m is not None and m.stage2 == "ok"


class TestGateCheck:
    def _cmdline(self, tmp_path, text):
        p = tmp_path / "cmdline"
        p.write_text(text)
        return str(p)

    def test_legacy_passes(self, tmp_path):
        ok, msg = bm.check_boot_markers(self._cmdline(tmp_path, BASE))
        assert ok
        assert "FAIL" not in msg

    def test_ok_passes(self, tmp_path):
        ok, msg = bm.check_boot_markers(
            self._cmdline(tmp_path, "oe5xrx.stage2=ok oe5xrx.overlays=ok:a")
        )
        assert ok
        assert "FAIL" not in msg

    def test_failure_message_contains_fail_and_reason(self, tmp_path):
        ok, msg = bm.check_boot_markers(
            self._cmdline(tmp_path, "oe5xrx.stage2=ok oe5xrx.overlays=failed:oe5xrx-ina226")
        )
        assert not ok
        assert "FAIL" in msg
        assert "oe5xrx-ina226" in msg

    def test_missing_cmdline_without_procfs_does_not_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bm, "PROCFS_PROBE", str(tmp_path / "no-proc"))
        ok, msg = bm.check_boot_markers(str(tmp_path / "missing"))
        assert ok
        assert "FAIL" not in msg

    def test_hidden_cmdline_with_procfs_fails_closed(self, tmp_path, monkeypatch):
        # ProcSubset=pid: /proc/self/stat exists, /proc/cmdline gives ENOENT.
        probe = tmp_path / "stat"
        probe.write_text("1 (init)\n")
        monkeypatch.setattr(bm, "PROCFS_PROBE", str(probe))
        ok, msg = bm.check_boot_markers(str(tmp_path / "missing"))
        assert not ok
        assert "FAIL" in msg

    def test_unreadable_existing_cmdline_fails_closed(self, tmp_path):
        # A directory raises IsADirectoryError: exists, but cannot be read.
        ok, msg = bm.check_boot_markers(str(tmp_path))
        assert not ok
        assert "FAIL" in msg

    def test_permission_error_fails_closed(self, tmp_path, monkeypatch):
        def deny(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("builtins.open", deny)
        ok, msg = bm.check_boot_markers(str(tmp_path / "cmdline"))
        assert not ok
        assert "PermissionError" in msg


class TestRunHealthChecksWiring:
    @pytest.fixture(autouse=True)
    def _stub_checks(self, monkeypatch):
        from station_agent import health_check as hc

        monkeypatch.setattr(hc, "_check_disk", lambda: (True, "Disk OK"))
        monkeypatch.setattr(hc, "_check_systemd_service", lambda: (True, "Systemd OK"))
        self.hc = hc

    def test_boot_gate_is_off_by_default(self, monkeypatch, tmp_path):
        cmdline = tmp_path / "cmdline"
        cmdline.write_text("oe5xrx.stage2=failed\n")
        monkeypatch.setattr(bm, "CMDLINE_PATH", str(cmdline))
        passed, messages = self.hc.run_health_checks()
        assert passed
        assert not any(m.startswith("Boot") for m in messages)
        passed, messages = self.hc.run_health_checks(boot_gate=True)
        assert not passed
        assert any("oe5xrx.stage2=failed" in m for m in messages)

    def test_raising_check_yields_non_empty_fail_reason(self, monkeypatch):
        def boom():
            raise RuntimeError("x" * 1000)

        monkeypatch.setattr(self.hc, "check_boot_markers", boom)
        passed, messages = self.hc.run_health_checks(boot_gate=True)
        assert not passed
        (fail,) = [m for m in messages if "FAIL" in m]
        assert fail.startswith("boot FAIL: unexpected exception: RuntimeError")
        assert len(fail) < 300
