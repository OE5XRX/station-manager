"""Unit tests for the agent's OTA orchestration — _verify_and_commit's
post-reboot guards and _handle_ota's reboot transition.

_handle_ota no longer calls _verify_and_commit inline from the fresh
install path; it reboots instead and verify-and-commit fires only via
the post-reboot recovery path at the top of _handle_ota
(deployment_result_status in {"rebooting", "verifying"}). The
upgrade_available guard must enforce rolled_back there.
"""

from __future__ import annotations

from station_agent.agent import StationAgent


class _FakeHttpClient:
    """Captures every report_status body for assertions."""

    def __init__(self):
        self.status_updates: list[dict] = []

    def request(self, method, path, **kwargs):
        if "status" in path:
            self.status_updates.append({"path": path, "json_data": kwargs.get("json_data")})

        class _Resp:
            status_code = 200

        return _Resp()


class _FakeConfig:
    server_url = "http://localhost"
    bootloader = "grub"


class TestVerifyAndCommitTrialGuard:
    def test_rolls_back_when_upgrade_available_is_zero(self, monkeypatch):
        """The bootloader already rolled the trial back (e.g. grub-ab
        detected bootcount > bootlimit and swapped boot_part). The
        /etc/os-release version may match for a same-version redeploy,
        so we need the trial-flag guard as a second signal."""
        monkeypatch.setattr("station_agent.agent.get_current_version", lambda: "v2")
        monkeypatch.setattr("station_agent.agent.get_bootloader", lambda _cfg: "grub")
        monkeypatch.setattr(
            "station_agent.agent.get_env",
            lambda _bl, key: "0" if key == "upgrade_available" else None,
        )
        # Health checks would pass if we got that far — but we
        # should short-circuit before them.
        monkeypatch.setattr(
            "station_agent.agent.run_health_checks",
            lambda **kw: (True, ["Network OK", "Disk OK"]),
        )
        commit_boot_called = []
        monkeypatch.setattr(
            "station_agent.agent.commit_boot",
            lambda *args, **kw: commit_boot_called.append(True) or True,
        )

        client = _FakeHttpClient()
        agent = StationAgent()
        agent._verify_and_commit(_FakeConfig(), client, result_pk=7, version="v2")

        # Last status update was rolled_back with the expected reason.
        last = client.status_updates[-1]["json_data"]
        assert last["status"] == "rolled_back"
        assert "upgrade_available" in last["error_message"]
        assert commit_boot_called == []

    def test_rolls_back_when_upgrade_available_is_missing(self, monkeypatch):
        """Env read returned None — fail closed rather than commit
        based on incomplete state."""
        monkeypatch.setattr("station_agent.agent.get_current_version", lambda: "v2")
        monkeypatch.setattr("station_agent.agent.get_bootloader", lambda _cfg: "grub")
        monkeypatch.setattr("station_agent.agent.get_env", lambda _bl, _key: None)
        monkeypatch.setattr(
            "station_agent.agent.run_health_checks",
            lambda **kw: (True, ["Network OK"]),
        )
        commit_boot_called = []
        monkeypatch.setattr(
            "station_agent.agent.commit_boot",
            lambda *a, **kw: commit_boot_called.append(True) or True,
        )

        client = _FakeHttpClient()
        agent = StationAgent()
        agent._verify_and_commit(_FakeConfig(), client, result_pk=7, version="v2")

        last = client.status_updates[-1]["json_data"]
        assert last["status"] == "rolled_back"
        assert commit_boot_called == []

    def test_commits_when_upgrade_available_is_one_and_version_matches(self, monkeypatch):
        """Happy path: trial flag is set, version matches target,
        health checks pass → commit_boot fires."""
        monkeypatch.setattr("station_agent.agent.get_current_version", lambda: "v2")
        monkeypatch.setattr("station_agent.agent.get_bootloader", lambda _cfg: "grub")
        monkeypatch.setattr(
            "station_agent.agent.get_env",
            lambda _bl, key: "1" if key == "upgrade_available" else None,
        )
        monkeypatch.setattr(
            "station_agent.agent.run_health_checks",
            lambda **kw: (True, ["Network OK"]),
        )
        commit_boot_called = []
        monkeypatch.setattr(
            "station_agent.agent.commit_boot",
            lambda *a, **kw: commit_boot_called.append(True) or True,
        )

        client = _FakeHttpClient()
        agent = StationAgent()
        agent._verify_and_commit(_FakeConfig(), client, result_pk=7, version="v2")

        assert commit_boot_called == [True]


class TestHandleOtaRebootTransition:
    """After install succeeds, _handle_ota must report 'rebooting'
    then invoke systemctl reboot. It must NOT call _verify_and_commit
    inline — that used to be the dev-mode simulation and now lives
    exclusively in the post-reboot-recovery path."""

    def _wire_successful_install(self, monkeypatch, calls):
        """Monkeypatch the network + install side of _handle_ota so
        execution reaches the reboot transition without doing real
        work. `calls` is a dict the test fills for assertions."""
        monkeypatch.setattr(
            "station_agent.agent.check_for_update",
            lambda cfg, http: {
                "deployment_result_id": 42,
                "download_url": "/api/v1/deployments/99/download/",
                "target_tag": "v2",
                "checksum_sha256": "a" * 64,
                "size_bytes": 1,
                "deployment_result_status": "pending",
            },
        )
        monkeypatch.setattr(
            "station_agent.agent.report_status",
            lambda cfg, http, pk, status, error_message="": calls.setdefault(
                "status_updates", []
            ).append((pk, status)),
        )
        monkeypatch.setattr(
            "station_agent.agent.download_firmware_resumable",
            lambda **kw: True,
        )
        monkeypatch.setattr("station_agent.agent.apply_update", lambda cfg, path: True)

        # If _verify_and_commit got called inline, record it — the
        # test asserts the list stays empty.
        def _explode_on_inline_verify(self_, *a, **kw):
            calls.setdefault("verify_calls", []).append(True)

        monkeypatch.setattr(StationAgent, "_verify_and_commit", _explode_on_inline_verify)

    def test_handle_ota_reboots_via_systemctl(self, monkeypatch):
        calls = {}
        self._wire_successful_install(monkeypatch, calls)

        run_args = []

        def fake_run(argv, **kwargs):
            run_args.append((tuple(argv), kwargs))

            class _CP:
                returncode = 0

            return _CP()

        monkeypatch.setattr("station_agent.agent.subprocess.run", fake_run)

        agent = StationAgent()
        # Pre-set the shutdown event so the post-reboot wait returns
        # immediately — simulates systemd signalling shutdown promptly
        # after systemctl reboot queued the restart.
        agent._shutdown.set()

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        agent._handle_ota(_Cfg(), object())

        # systemctl reboot was called exactly once.
        assert run_args, "subprocess.run was never called"
        assert run_args[0][0] == ("systemctl", "reboot")

        # 'rebooting' was reported. No 'failed' should follow on the
        # happy path — the safety-net only fires if the shutdown event
        # times out.
        status_sequence = [s for _pk, s in calls["status_updates"]]
        assert status_sequence[-1] == "rebooting"
        assert "failed" not in status_sequence

    def test_handle_ota_does_not_call_verify_inline(self, monkeypatch):
        calls = {}
        self._wire_successful_install(monkeypatch, calls)
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )

        agent = StationAgent()
        # Pre-set shutdown so the post-reboot wait returns immediately
        # instead of blocking the test suite for 5 minutes.
        agent._shutdown.set()

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        agent._handle_ota(_Cfg(), object())

        assert calls.get("verify_calls", []) == []

    def test_handle_ota_reports_failed_when_reboot_errors(self, monkeypatch):
        calls = {}
        self._wire_successful_install(monkeypatch, calls)

        def failing_run(argv, **kw):
            raise FileNotFoundError("no systemctl")

        monkeypatch.setattr("station_agent.agent.subprocess.run", failing_run)

        agent = StationAgent()

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        agent._handle_ota(_Cfg(), object())

        # Status sequence: rebooting (first, optimistic), then failed
        # after the reboot call raised.
        status_sequence = [s for _pk, s in calls["status_updates"]]
        assert status_sequence[-1] == "failed"

    def test_handle_ota_reports_failed_when_reboot_times_out(self, monkeypatch):
        """If systemctl returns 0 but systemd never signals shutdown
        within the timeout, report FAILED so the operator sees the
        station stuck on 'rebooting' isn't actually going to reboot."""
        calls = {}
        self._wire_successful_install(monkeypatch, calls)

        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )

        class _TimeoutEvent:
            """Emulates threading.Event.wait returning False (timeout).
            We don't actually sleep 5 minutes — we just tell the wait
            it hit the timeout."""

            def set(self):
                pass

            def clear(self):
                pass

            def is_set(self):
                return False

            def wait(self, timeout=None):
                return False

        agent = StationAgent()
        agent._shutdown = _TimeoutEvent()

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        agent._handle_ota(_Cfg(), object())

        status_sequence = [s for _pk, s in calls["status_updates"]]
        assert status_sequence[-1] == "failed"


class TestDestPathAndLegacySweep:
    """The download artifact is the extracted rootfs (not the full
    wic) since server-side PR #28. The local filename needs to match
    what's actually there, and any legacy .wic.bz2 partials in the
    download dir get purged once so they don't accumulate."""

    def test_dest_path_uses_rootfs_suffix(self, monkeypatch, tmp_path):
        download_dir = tmp_path / "station-agent"
        download_dir.mkdir()
        captured = {}

        monkeypatch.setattr(
            "station_agent.agent.check_for_update",
            lambda cfg, http: {
                "deployment_result_id": 42,
                "download_url": "/api/v1/deployments/99/download/",
                "target_tag": "v2",
                "checksum_sha256": "a" * 64,
                "size_bytes": 1,
                "deployment_result_status": "pending",
            },
        )
        monkeypatch.setattr("station_agent.agent.report_status", lambda *a, **kw: None)

        def fake_download(**kw):
            captured["dest_path"] = kw["dest_path"]
            return True

        monkeypatch.setattr("station_agent.agent.download_firmware_resumable", fake_download)
        monkeypatch.setattr("station_agent.agent.apply_update", lambda cfg, path: True)
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )
        monkeypatch.setattr(
            StationAgent,
            "_verify_and_commit",
            lambda *a, **kw: None,
        )

        _download_dir_str = str(download_dir)

        class _Cfg:
            download_dir = _download_dir_str
            server_url = "http://localhost"

        agent = StationAgent()
        # Pre-set shutdown so the post-reboot wait returns immediately.
        agent._shutdown.set()
        agent._handle_ota(_Cfg(), object())

        assert captured["dest_path"].endswith(".rootfs.bz2")
        assert "wic.bz2" not in captured["dest_path"]

    def test_legacy_wic_partials_are_swept_before_download(self, monkeypatch, tmp_path):
        download_dir = tmp_path / "station-agent"
        download_dir.mkdir()
        # Seed a legacy partial from the previous agent version.
        legacy = download_dir / "firmware-v1.wic.bz2"
        legacy.write_bytes(b"stale bytes")

        # Any other unrelated file stays.
        keep = download_dir / "other-file.txt"
        keep.write_bytes(b"keep me")

        monkeypatch.setattr(
            "station_agent.agent.check_for_update",
            lambda cfg, http: {
                "deployment_result_id": 42,
                "download_url": "/api/v1/deployments/99/download/",
                "target_tag": "v2",
                "checksum_sha256": "a" * 64,
                "size_bytes": 1,
                "deployment_result_status": "pending",
            },
        )
        monkeypatch.setattr("station_agent.agent.report_status", lambda *a, **kw: None)
        monkeypatch.setattr("station_agent.agent.download_firmware_resumable", lambda **kw: True)
        monkeypatch.setattr("station_agent.agent.apply_update", lambda cfg, path: True)
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )
        monkeypatch.setattr(
            StationAgent,
            "_verify_and_commit",
            lambda *a, **kw: None,
        )

        _download_dir_str = str(download_dir)

        class _Cfg:
            download_dir = _download_dir_str
            server_url = "http://localhost"

        agent = StationAgent()
        # Pre-set shutdown so the post-reboot wait returns immediately.
        agent._shutdown.set()
        agent._handle_ota(_Cfg(), object())

        assert not legacy.exists(), "legacy .wic.bz2 partial was not removed"
        assert keep.exists(), "unrelated file must stay"
