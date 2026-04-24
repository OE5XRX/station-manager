"""Unit tests for station_agent.bootloader slot detection.

Slot detection is bootloader-agnostic: both oe5xrx-grub.cfg and
boot.cmd (u-boot) emit `root=PARTLABEL=root_${boot_part}` to the
kernel cmdline, so one regex handles both.
"""

from __future__ import annotations

import subprocess

import pytest

from station_agent import bootloader


def _mock_proc_cmdline(monkeypatch, tmp_path, text: str) -> None:
    """Redirect open('/proc/cmdline') to a tmp file with ``text``."""
    fake = tmp_path / "cmdline"
    fake.write_text(text)
    original_open = open

    def patched(path, *args, **kwargs):
        if path == "/proc/cmdline":
            return original_open(fake, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched)


class TestSlotFromCmdline:
    def test_grub_cmdline_parses_slot_a(self, monkeypatch, tmp_path):
        """Matches oe5xrx-grub.cfg:39 — `root=PARTLABEL=root_a`."""
        cmdline = (
            "/bzImage root=PARTLABEL=root_a ro rootwait console=ttyS0,115200 net.ifnames=0 panic=5"
        )
        _mock_proc_cmdline(monkeypatch, tmp_path, cmdline)
        assert bootloader._slot_from_cmdline() == "a"

    def test_uboot_cmdline_parses_slot_b(self, monkeypatch, tmp_path):
        """Matches boot.cmd:57 — `root=PARTLABEL=root_b`."""
        cmdline = (
            "root=PARTLABEL=root_b ro rootwait console=serial0,115200 "
            "console=tty1 fsck.repair=yes net.ifnames=0"
        )
        _mock_proc_cmdline(monkeypatch, tmp_path, cmdline)
        assert bootloader._slot_from_cmdline() == "b"

    def test_non_partlabel_cmdline_returns_none(self, monkeypatch, tmp_path):
        """An older image may boot via `root=/dev/sda2` — the probe
        returns None so the fallback can take over."""
        cmdline = "root=/dev/sda2 ro rootwait"
        _mock_proc_cmdline(monkeypatch, tmp_path, cmdline)
        assert bootloader._slot_from_cmdline() is None

    def test_unreadable_proc_returns_none(self, monkeypatch):
        def raiser(*args, **kwargs):
            raise OSError("no /proc")

        monkeypatch.setattr("builtins.open", raiser)
        assert bootloader._slot_from_cmdline() is None


class TestSlotFromRootMount:
    def test_matches_partlabel_root_a(self, monkeypatch):
        """Compare the device backing / against the partlabel
        symlinks. Handles the edge case where cmdline was
        rewritten or doesn't carry root=."""

        class _FakeStat:
            def __init__(self, st_dev: int = 0, st_rdev: int = 0):
                self.st_dev = st_dev
                self.st_rdev = st_rdev

        _root_dev = (8 << 8) | 1  # major 8, minor 1
        _root_a_rdev = (8 << 8) | 1
        _root_b_rdev = (8 << 8) | 2

        def fake_stat(path):
            if path == "/":
                return _FakeStat(st_dev=_root_dev)
            if path == "/dev/disk/by-partlabel/root_a":
                return _FakeStat(st_rdev=_root_a_rdev)
            if path == "/dev/disk/by-partlabel/root_b":
                return _FakeStat(st_rdev=_root_b_rdev)
            raise FileNotFoundError(path)

        monkeypatch.setattr("os.stat", fake_stat)
        assert bootloader._slot_from_root_mount() == "a"

    def test_partlabel_symlinks_missing_returns_none(self, monkeypatch):
        def fake_stat(path):
            if path == "/":

                class _FakeStat:
                    st_dev = 123

                return _FakeStat()
            raise FileNotFoundError(path)

        monkeypatch.setattr("os.stat", fake_stat)
        assert bootloader._slot_from_root_mount() is None

    def test_root_stat_fails_returns_none(self, monkeypatch):
        def fake_stat(path):
            raise OSError("no /")

        monkeypatch.setattr("os.stat", fake_stat)
        assert bootloader._slot_from_root_mount() is None


class TestGetActiveSlot:
    def test_cmdline_wins_over_mount(self, monkeypatch):
        """Primary probe's answer is authoritative."""
        monkeypatch.setattr(bootloader, "_slot_from_cmdline", lambda: "b")
        monkeypatch.setattr(bootloader, "_slot_from_root_mount", lambda: "a")
        assert bootloader.get_active_slot() == "b"

    def test_falls_back_to_mount(self, monkeypatch):
        monkeypatch.setattr(bootloader, "_slot_from_cmdline", lambda: None)
        monkeypatch.setattr(bootloader, "_slot_from_root_mount", lambda: "a")
        assert bootloader.get_active_slot() == "a"

    def test_raises_when_both_probes_fail(self, monkeypatch):
        monkeypatch.setattr(bootloader, "_slot_from_cmdline", lambda: None)
        monkeypatch.setattr(bootloader, "_slot_from_root_mount", lambda: None)
        with pytest.raises(RuntimeError, match=r"refusing to guess"):
            bootloader.get_active_slot()

    def test_bootloader_arg_ignored(self, monkeypatch):
        """API compatibility: the ``bootloader`` parameter is kept
        for callers that still pass it, but detection is
        bootloader-agnostic."""
        monkeypatch.setattr(bootloader, "_slot_from_cmdline", lambda: "a")
        assert bootloader.get_active_slot("grub") == "a"
        assert bootloader.get_active_slot("uboot") == "a"
        assert bootloader.get_active_slot() == "a"


class TestApplyUpdatePropagatesSlotError:
    """If ``get_active_slot`` can't determine the slot, ``apply_update``
    lets the RuntimeError propagate so ``agent._handle_ota`` can
    forward the specific reason into the server-visible
    ``error_message``. A generic ``False`` return would have lumped
    slot-detection failures in with write failures under the
    misleading "Failed to write firmware..." message."""

    def test_apply_update_propagates_runtime_error_on_indeterminate_slot(
        self, monkeypatch, tmp_path
    ):
        from station_agent import ota

        def blow_up(_bl):
            raise RuntimeError("cannot determine active slot")

        # get_inactive_slot delegates to get_active_slot; patching
        # the downstream one is enough.
        monkeypatch.setattr("station_agent.ota.get_inactive_slot", blow_up)

        class _FakeConfig:
            pass

        firmware = tmp_path / "firmware.rootfs.bz2"
        firmware.write_bytes(b"")

        with pytest.raises(RuntimeError, match="cannot determine active slot"):
            ota.apply_update(_FakeConfig(), str(firmware))


class TestEnvToolTimeouts:
    """A wedged grub-editenv / fw_printenv / fw_setenv must not hang
    the agent. The trial-flag guard in ``_verify_and_commit`` calls
    ``get_env`` synchronously, and ``_run`` is on the commit path —
    either blocking indefinitely would leave the agent in VERIFYING
    forever without ever reporting ``rolled_back``/``failed``.
    """

    def test_get_env_grub_timeout_returns_none(self, monkeypatch):
        def timeout_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(bootloader.subprocess, "run", timeout_run)
        assert bootloader.get_env("grub", "bootcount") is None

    def test_get_env_uboot_timeout_returns_none(self, monkeypatch):
        def timeout_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(bootloader.subprocess, "run", timeout_run)
        assert bootloader.get_env("uboot", "upgrade_available") is None

    def test_get_env_missing_tool_returns_none(self, monkeypatch):
        def missing(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(bootloader.subprocess, "run", missing)
        assert bootloader.get_env("grub", "bootcount") is None

    def test_get_env_passes_timeout_kwarg(self, monkeypatch):
        """Regression guard: if someone drops the ``timeout=`` kwarg
        on the subprocess call, this test fails loudly rather than
        silently reintroducing the hang."""
        seen = {}

        class _Result:
            returncode = 0
            stdout = ""

        def spy(cmd, **kwargs):
            seen["kwargs"] = kwargs
            return _Result()

        monkeypatch.setattr(bootloader.subprocess, "run", spy)
        bootloader.get_env("grub", "bootcount")
        assert seen["kwargs"].get("timeout") == bootloader._ENV_TOOL_TIMEOUT

    def test_run_timeout_returns_false(self, monkeypatch):
        def timeout_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 10))

        monkeypatch.setattr(bootloader.subprocess, "run", timeout_run)
        assert bootloader._run(["grub-editenv", "/boot/x", "set", "a=b"]) is False

    def test_run_permission_error_returns_false(self, monkeypatch):
        """If the env tool is present but not executable (wrong
        perms / noexec mount / SELinux denial), ``subprocess.run``
        raises ``PermissionError`` before the process even starts.
        ``_run`` must return False, not let it crash
        ``set_upgrade_pending`` / ``commit_boot_local`` — otherwise
        ``_handle_ota`` can't report FAILED."""

        def denied(cmd, **kwargs):
            raise PermissionError(13, "Permission denied", cmd[0])

        monkeypatch.setattr(bootloader.subprocess, "run", denied)
        assert bootloader._run(["grub-editenv", "/boot/x", "set", "a=b"]) is False

    def test_get_env_permission_error_returns_none(self, monkeypatch):
        """Same scenario for the read path. ``get_env`` returns None
        so the ``_verify_and_commit`` guard treats it as unreadable
        and fails closed to rolled_back."""

        def denied(cmd, **kwargs):
            raise PermissionError(13, "Permission denied", cmd[0])

        monkeypatch.setattr(bootloader.subprocess, "run", denied)
        assert bootloader.get_env("grub", "bootcount") is None


class TestEnvWritesAreAtomic:
    """``set_upgrade_pending`` and ``commit_boot_local`` must write
    all their keys in a single tool invocation. A crash or power loss
    between writes would otherwise leave the station in a half-staged
    trial state (e.g. ``boot_part`` flipped but ``upgrade_available``
    still 0 — the station would boot the new slot with no verify or
    rollback path).
    """

    def _capture_calls(self, monkeypatch):
        calls = []

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def record(cmd, **kwargs):
            calls.append(list(cmd))
            return _Result()

        monkeypatch.setattr(bootloader.subprocess, "run", record)
        return calls

    def test_set_upgrade_pending_grub_uses_single_call(self, monkeypatch):
        calls = self._capture_calls(monkeypatch)
        assert bootloader.set_upgrade_pending("grub", "b") is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[:3] == [bootloader.GRUB_ENV_TOOL, bootloader.GRUB_ENV_PATH, "set"]
        # Order-independent — grub-editenv accepts multiple KEY=VALUE
        # pairs in one invocation.
        assert set(cmd[3:]) == {"boot_part=b", "upgrade_available=1", "bootcount=0"}

    def test_commit_boot_local_grub_uses_single_call(self, monkeypatch):
        calls = self._capture_calls(monkeypatch)
        assert bootloader.commit_boot_local("grub") is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[:3] == [bootloader.GRUB_ENV_TOOL, bootloader.GRUB_ENV_PATH, "set"]
        assert set(cmd[3:]) == {"bootcount=0", "upgrade_available=0"}

    def test_set_upgrade_pending_uboot_uses_single_call(self, monkeypatch):
        calls = self._capture_calls(monkeypatch)
        assert bootloader.set_upgrade_pending("uboot", "a") is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == bootloader.UBOOT_ENV_TOOL
        # fw_setenv takes alternating KEY VALUE positional args. The
        # even-count assertion catches a future regression that sneaks
        # in an unpaired trailing arg — zip would silently drop it and
        # the pairs-equality check below would still pass.
        assert (len(cmd) - 1) % 2 == 0, f"uboot cmd has odd arg count: {cmd}"
        pairs = dict(zip(cmd[1::2], cmd[2::2]))
        assert pairs == {"boot_part": "a", "upgrade_available": "1", "bootcount": "0"}

    def test_commit_boot_local_uboot_uses_single_call(self, monkeypatch):
        calls = self._capture_calls(monkeypatch)
        assert bootloader.commit_boot_local("uboot") is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == bootloader.UBOOT_ENV_TOOL
        assert (len(cmd) - 1) % 2 == 0, f"uboot cmd has odd arg count: {cmd}"
        pairs = dict(zip(cmd[1::2], cmd[2::2]))
        assert pairs == {"bootcount": "0", "upgrade_available": "0"}
