"""Unit tests for the agent's OTA orchestration — _verify_and_commit's
post-reboot guards and _handle_ota's reboot transition.

_verify_and_commit is called both inline from a fresh install
(pre-reboot code path, soon to go away) and from the post-reboot
recovery path. The upgrade_available guard must work in both.
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
