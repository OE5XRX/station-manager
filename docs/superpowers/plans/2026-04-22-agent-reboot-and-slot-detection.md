# Agent-side OTA hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `station_agent` actually reboot after install, detect the active A/B slot from the running rootfs (not the mutable bootloader env), and reject commit when the bootloader already rolled back the trial.

**Architecture:** Four agent-side fixes in `station_agent/bootloader.py` and `station_agent/agent.py`. `get_active_slot` becomes runtime-derived via `/proc/cmdline` + root-mount fallback; `apply_update` lets the `RuntimeError` propagate; `_handle_ota` catches it and forwards the message into the server-visible `error_message`. `_verify_and_commit` gains an `upgrade_available == "1"` guard from the existing `get_env` helper. `_handle_ota` issues a real `systemctl reboot` and drops its inline verify call (the existing post-reboot-recovery path in `agent.py:68-78` is the sole verify entrypoint). `dest_path` renamed to `.rootfs.bz2` with a one-time sweep of legacy `.wic.bz2` partials.

**Tech Stack:** Python >=3.10 (CI runs on 3.14), stdlib `subprocess` / `os` / `re`, pytest + monkeypatch. No new deps, no server-side changes, no Yocto recipe changes.

**Design spec:** `docs/superpowers/specs/2026-04-22-agent-reboot-and-slot-detection-design.md`

---

## Task 1: Runtime-derived `get_active_slot` + `apply_update` RuntimeError catch

**Goal:** Replace the bootloader-env read in `get_active_slot` with two runtime probes (cmdline first, root-mount fallback). Fail closed if neither resolves. Teach `apply_update` to report FAILED instead of crashing on an indeterminate slot.

**Files:**
- Modify: `station_agent/bootloader.py`
- Modify: `station_agent/ota.py` (`apply_update`)
- Create: `tests/test_bootloader.py`

### Steps

- [ ] **Step 1: Write the failing cmdline-parse tests**

Create `tests/test_bootloader.py`:

```python
"""Unit tests for station_agent.bootloader slot detection.

Slot detection is bootloader-agnostic: both oe5xrx-grub.cfg and
boot.cmd (u-boot) emit `root=PARTLABEL=root_${boot_part}` to the
kernel cmdline, so one regex handles both.
"""

from __future__ import annotations

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
            "/bzImage root=PARTLABEL=root_a ro rootwait "
            "console=ttyS0,115200 net.ifnames=0 panic=5"
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
```

- [ ] **Step 2: Run the test — expect ImportError / AttributeError**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestSlotFromCmdline -v
```

Expected: FAIL — `_slot_from_cmdline` doesn't exist yet.

- [ ] **Step 3: Implement the cmdline probe + module-level regex**

Replace the top of `station_agent/bootloader.py` (keeping existing imports and helpers, but adding the new imports / constants and the new helper; do NOT touch `get_env`, `commit_boot_local`, `set_upgrade_pending` yet):

```python
"""Bootloader abstraction for A/B boot management.

Supports U-Boot (fw_setenv/fw_printenv) and GRUB (grub-editenv).
The active backend is chosen via config.yml's 'bootloader' field.

Active-slot detection is intentionally bootloader-agnostic: both
oe5xrx-grub.cfg and boot.cmd emit the same
``root=PARTLABEL=root_${boot_part}`` cmdline anchor, so one regex
over ``/proc/cmdline`` handles both. Env-based detection would
misreport the active slot in every "set_upgrade_pending-but-not-
yet-rebooted" window and risk ``install_to_slot`` overwriting the
live rootfs.
"""

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

GRUB_ENV_PATH = "/boot/EFI/BOOT/grubenv"
UBOOT_ENV_TOOL = "fw_setenv"
UBOOT_PRINT_TOOL = "fw_printenv"
GRUB_ENV_TOOL = "grub-editenv"

_CMDLINE_PATTERN = re.compile(r"root=PARTLABEL=root_([ab])\b")
```

Then, anywhere below `_detect_bootloader` and above `get_active_slot`, add:

```python
def _slot_from_cmdline() -> str | None:
    """Primary probe: parse the A/B slot from /proc/cmdline.

    Works for both grub (oe5xrx-grub.cfg) and u-boot (boot.cmd)
    because the wic recipe labels partitions uniformly and both boot
    scripts set ``root=PARTLABEL=root_${boot_part}`` in the kernel
    cmdline. Returns None if /proc/cmdline can't be read or doesn't
    contain a PARTLABEL anchor (older images may boot via
    ``root=/dev/sda2``; the caller falls back to mount inspection).
    """
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read()
    except OSError:
        return None
    m = _CMDLINE_PATTERN.search(cmdline)
    return m.group(1) if m else None
```

- [ ] **Step 4: Run the test to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestSlotFromCmdline -v
```

Expected: 4 passed.

- [ ] **Step 5: Write the failing mount-fallback tests**

Append to `tests/test_bootloader.py`:

```python
class TestSlotFromRootMount:
    def test_matches_partlabel_root_a(self, monkeypatch):
        """Compare the device backing / against the partlabel
        symlinks. Handles the edge case where cmdline was
        rewritten or doesn't carry root=."""

        class _FakeStat:
            def __init__(self, st_dev: int = 0, st_rdev: int = 0):
                self.st_dev = st_dev
                self.st_rdev = st_rdev

        _root_dev = (8 << 8) | 1   # major 8, minor 1
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
```

- [ ] **Step 6: Run the test — expect AttributeError**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestSlotFromRootMount -v
```

Expected: FAIL — `_slot_from_root_mount` doesn't exist.

- [ ] **Step 7: Implement the mount-fallback probe**

Add to `station_agent/bootloader.py`, right below `_slot_from_cmdline`:

```python
def _slot_from_root_mount() -> str | None:
    """Fallback probe: compare ``os.stat('/').st_dev`` to the
    device nodes behind the partlabel symlinks. Handles the edge
    case where the cmdline anchor got stripped (initramfs
    rewrite, kernel args injected by a service).
    """
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        return None
    for slot in ("a", "b"):
        try:
            part = os.stat(f"/dev/disk/by-partlabel/root_{slot}")
        except OSError:
            continue
        if part.st_rdev == root_dev:
            return slot
    return None
```

- [ ] **Step 8: Run the test to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestSlotFromRootMount -v
```

Expected: 3 passed.

- [ ] **Step 9: Write the failing `get_active_slot` tests**

Append to `tests/test_bootloader.py`:

```python
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
```

- [ ] **Step 10: Run the test — expect FAIL (current `get_active_slot` reads env)**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestGetActiveSlot -v
```

Expected: at least `test_raises_when_both_probes_fail` fails (today's code returns `"a"` fallback string).

- [ ] **Step 11: Rewrite `get_active_slot`**

In `station_agent/bootloader.py`, replace the body of the existing `get_active_slot`:

```python
def get_active_slot(bootloader: str = "") -> str:
    """Return the currently-running A/B slot.

    Derived from the running rootfs, NOT from the bootloader env.
    The env's ``boot_part`` is a "next-boot hint" that
    ``set_upgrade_pending`` mutates before reboot; reading it would
    misidentify the active slot in every "set_upgrade_pending-but-
    not-yet-rebooted" window and let ``install_to_slot`` overwrite
    the running rootfs.

    Tries ``/proc/cmdline`` first, then the root-mount / partlabel
    comparison. Raises ``RuntimeError`` if neither resolves — fail
    closed, because the alternative (guessing a default slot)
    risks corrupting the running system.

    The ``bootloader`` argument is kept for API compatibility but
    ignored — detection is bootloader-agnostic.
    """
    for probe in (_slot_from_cmdline, _slot_from_root_mount):
        slot = probe()
        if slot in ("a", "b"):
            return slot
    raise RuntimeError(
        "Cannot determine active A/B slot from /proc/cmdline or / mount. "
        "Refusing to guess — next install_to_slot could corrupt the running rootfs."
    )
```

- [ ] **Step 12: Run the `get_active_slot` tests to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestGetActiveSlot -v
```

Expected: 4 passed.

- [ ] **Step 13: Write the failing `apply_update` RuntimeError-catch test**

Append to `tests/test_bootloader.py`:

```python
class TestApplyUpdateHandlesRuntimeError:
    """If ``get_active_slot`` can't determine the slot, ``apply_update``
    must report FAILED rather than propagate RuntimeError and crash
    the worker loop."""

    def test_apply_update_returns_false_when_slot_indeterminate(
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

        assert ota.apply_update(_FakeConfig(), str(firmware)) is False
```

- [ ] **Step 14: Run the test — expect FAIL (RuntimeError propagates)**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py::TestApplyUpdateHandlesRuntimeError -v
```

Expected: FAIL with `RuntimeError`.

- [ ] **Step 15: Catch `RuntimeError` in `apply_update`**

In `station_agent/ota.py`, find the `apply_update` function (around line 400). Replace the `bl = get_bootloader(config)` / `target_slot = get_inactive_slot(bl)` block with:

```python
    bl = get_bootloader(config)
    try:
        target_slot = get_inactive_slot(bl)
    except RuntimeError as exc:
        # get_active_slot fails closed when the running slot can't be
        # identified. Propagating the RuntimeError would kill the
        # worker loop — instead report FAILED via the normal False
        # return so the caller (agent._handle_ota) reports it to the
        # server.
        logger.error("Cannot determine inactive slot: %s", exc)
        return False
    target_dev = f"/dev/disk/by-partlabel/root_{target_slot}"
```

- [ ] **Step 16: Run the test to verify PASS + full suite green**

```
cd ~/station-manager && .venv/bin/pytest tests/test_bootloader.py -v 2>&1 | tail -15
cd ~/station-manager && .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: `test_bootloader.py` shows all tests passing (12 total). Full suite: no regressions vs the previous 212.

- [ ] **Step 17: Ruff**

```
cd ~/station-manager && .venv/bin/ruff format station_agent/bootloader.py station_agent/ota.py tests/test_bootloader.py && \
  .venv/bin/ruff check station_agent/bootloader.py station_agent/ota.py tests/test_bootloader.py
```

Expected: `All checks passed!`.

- [ ] **Step 18: Commit**

```
cd ~/station-manager && \
  git add station_agent/bootloader.py station_agent/ota.py tests/test_bootloader.py && \
  git commit -m "bootloader: derive get_active_slot from the running rootfs

The bootloader env's boot_part is a 'next-boot hint' that
set_upgrade_pending mutates before reboot. Reading it in
get_active_slot/get_inactive_slot returned the slot the kernel is
actually running from in every set_upgrade_pending-but-not-yet-
rebooted window, causing install_to_slot to overwrite the live
rootfs on the second OTA run — observed directly in the 2026-04-22
end-to-end test.

Switch to runtime probes: /proc/cmdline's root=PARTLABEL=root_X
anchor (emitted identically by oe5xrx-grub.cfg and boot.cmd, so
one regex handles both bootloaders), with os.stat('/').st_dev vs
partlabel-symlink st_rdev as a fallback. Both probes fail → raise
RuntimeError with a loud message. Fail closed — guessing a
default would be the brick vector we just saw.

The bootloader argument on get_active_slot is kept for API
compatibility but ignored.

apply_update catches the RuntimeError and returns False so
agent._handle_ota reports FAILED cleanly instead of crashing the
worker loop."
```

---

## Task 2: `upgrade_available == "1"` guard in `_verify_and_commit`

**Goal:** Detect bootloader-side rollback that the `/etc/os-release` version check can't see (e.g. same-version redeploy where both slots report the same tag). Fail closed if the env read returns None.

**Files:**
- Modify: `station_agent/agent.py`
- Create: `tests/test_agent_ota_flow.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_ota_flow.py`:

```python
"""Unit tests for the agent's OTA orchestration — _verify_and_commit's
post-reboot guards and _handle_ota's reboot transition.

_verify_and_commit is called both inline from a fresh install
(pre-reboot code path, soon to go away) and from the post-reboot
recovery path. The upgrade_available guard must work in both.
"""

from __future__ import annotations

import pytest

from station_agent.agent import StationAgent


class _FakeHttpClient:
    """Captures every report_status body for assertions."""

    def __init__(self):
        self.status_updates: list[dict] = []

    def request(self, method, path, **kwargs):
        if "status" in path:
            self.status_updates.append(
                {"path": path, "json_data": kwargs.get("json_data")}
            )

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
        monkeypatch.setattr(
            "station_agent.agent.get_current_version", lambda: "v2"
        )
        monkeypatch.setattr(
            "station_agent.agent.get_bootloader", lambda _cfg: "grub"
        )
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
        monkeypatch.setattr(
            "station_agent.agent.get_current_version", lambda: "v2"
        )
        monkeypatch.setattr(
            "station_agent.agent.get_bootloader", lambda _cfg: "grub"
        )
        monkeypatch.setattr(
            "station_agent.agent.get_env", lambda _bl, _key: None
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

        last = client.status_updates[-1]["json_data"]
        assert last["status"] == "rolled_back"
        assert commit_boot_called == []

    def test_commits_when_upgrade_available_is_one_and_version_matches(
        self, monkeypatch
    ):
        """Happy path: trial flag is set, version matches target,
        health checks pass → commit_boot fires."""
        monkeypatch.setattr(
            "station_agent.agent.get_current_version", lambda: "v2"
        )
        monkeypatch.setattr(
            "station_agent.agent.get_bootloader", lambda _cfg: "grub"
        )
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
```

- [ ] **Step 2: Run the tests — expect the two rollback tests to fail**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestVerifyAndCommitTrialGuard -v
```

Expected: the "rolls_back_when_upgrade_available_is_zero" and "is_missing" tests FAIL (today's code proceeds to commit). The happy-path test may pass or fail depending on mocks — not authoritative yet.

- [ ] **Step 3: Add the guard to `_verify_and_commit`**

In `station_agent/agent.py`, find `_verify_and_commit` (starts around line 153). Add two imports at the top of the function body:

```python
from .bootloader import get_bootloader, get_env
```

(Keep the existing `from .inventory import get_current_version` on the line below — it's already there. The new imports go alongside it.)

Then, between the existing version-mismatch check (ends around line 197 with a `return`) and the `passed, messages = run_health_checks(...)` line, insert:

```python
        # Trial-flag guard: both oe5xrx-grub.cfg and boot.cmd clear
        # upgrade_available to 0 when they roll back after
        # bootcount > bootlimit. Reading it post-reboot catches the
        # same-version-redeploy blind spot the version check above
        # cannot see (both slots return the same /etc/os-release
        # tag, so running_version == target is not a proof the
        # bootloader didn't swap back). None also means rolled_back
        # — env read failed and we refuse to commit blind.
        bl = get_bootloader(config)
        upgrade_available = get_env(bl, "upgrade_available")
        if upgrade_available != "1":
            report_status(
                config,
                http_client,
                result_pk,
                "rolled_back",
                error_message=(
                    f"Bootloader upgrade_available={upgrade_available!r} "
                    "(expected '1') — trial boot was rolled back or env "
                    "read failed; refusing to commit."
                ),
            )
            logger.warning(
                "Refusing to commit deployment %s: upgrade_available=%r",
                result_pk,
                upgrade_available,
            )
            return
```

Note: the existing module-level imports at the top of `agent.py` may already have `commit_boot` imported from `.ota`. Verify that `get_bootloader` is exported from `station_agent.bootloader`. If it isn't already module-level-imported in agent.py, add `from .bootloader import get_bootloader, get_env` to the imports at the top of the file (alongside the existing `.ota` imports) rather than re-importing inside the function. If you choose module-level, remove the in-function import.

- [ ] **Step 4: Run the tests to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestVerifyAndCommitTrialGuard -v
```

Expected: 3 passed.

- [ ] **Step 5: Full suite sanity check**

```
cd ~/station-manager && .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: no regressions.

- [ ] **Step 6: Ruff**

```
cd ~/station-manager && .venv/bin/ruff format station_agent/agent.py tests/test_agent_ota_flow.py && \
  .venv/bin/ruff check station_agent/agent.py tests/test_agent_ota_flow.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```
cd ~/station-manager && \
  git add station_agent/agent.py tests/test_agent_ota_flow.py && \
  git commit -m "agent: reject commit when bootloader already rolled back

_verify_and_commit relied on /etc/os-release alone to detect a
rollback. That catches v1->v2 rollbacks (wrong version running on
slot_a after bootloader swap), but misses same-version redeploys
where both slots carry the same tag — the rollback leaves the
running version matching the target and the commit proceeds
blindly.

Add an upgrade_available == '1' guard between the version check
and the health checks. Both oe5xrx-grub.cfg and boot.cmd clear
upgrade_available to 0 when they swap back after bootcount >
bootlimit, so reading it post-reboot is a reliable signal. None
from get_env (unreadable env) also triggers rolled_back — fail
closed on incomplete state is better than committing blind.

Three new tests cover: upgrade_available=0 rolls back,
upgrade_available missing rolls back, upgrade_available=1 with
version match commits."
```

---

## Task 3: Real `systemctl reboot` + remove inline verify

**Goal:** Replace the "in production, device would reboot now" simulation with a real `subprocess.run(["systemctl", "reboot"])`. Drop the inline `_verify_and_commit` call — the existing post-reboot-recovery path in `_handle_ota` is the sole verify entrypoint.

**Files:**
- Modify: `station_agent/agent.py`
- Modify: `tests/test_agent_ota_flow.py`

### Steps

- [ ] **Step 1: Write the failing reboot-transition tests**

Append to `tests/test_agent_ota_flow.py`:

```python
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
        monkeypatch.setattr(
            "station_agent.agent.apply_update", lambda cfg, path: True
        )
        # If _verify_and_commit got called inline, record it — the
        # test asserts the list stays empty.
        def _explode_on_inline_verify(self_, *a, **kw):
            calls.setdefault("verify_calls", []).append(True)

        monkeypatch.setattr(
            StationAgent, "_verify_and_commit", _explode_on_inline_verify
        )

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

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        # Need a minimal http_client fake — _handle_ota just hands
        # it to report_status/download_firmware_resumable, both
        # monkeypatched above.
        agent._handle_ota(_Cfg(), object())

        # systemctl reboot was called exactly once.
        assert run_args, "subprocess.run was never called"
        assert run_args[0][0] == ("systemctl", "reboot")

        # 'rebooting' was reported *before* the reboot call.
        status_sequence = [s for _pk, s in calls["status_updates"]]
        assert "rebooting" in status_sequence
        assert status_sequence.index("rebooting") == len(status_sequence) - 1

    def test_handle_ota_does_not_call_verify_inline(self, monkeypatch):
        calls = {}
        self._wire_successful_install(monkeypatch, calls)
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )

        agent = StationAgent()

        class _Cfg:
            download_dir = "/tmp/station-agent"
            server_url = "http://localhost"

        agent._handle_ota(_Cfg(), object())

        assert calls.get("verify_calls", []) == []

    def test_handle_ota_reports_failed_when_reboot_errors(self, monkeypatch):
        calls = {}
        self._wire_successful_install(monkeypatch, calls)

        import subprocess as _subproc

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
```

- [ ] **Step 2: Run the tests — expect FAIL**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestHandleOtaRebootTransition -v
```

Expected: all three tests fail — current code calls `_verify_and_commit` inline and doesn't invoke subprocess.

- [ ] **Step 3: Import subprocess at the top of `agent.py`**

Near the top of `station_agent/agent.py`, after the other stdlib imports, add `import subprocess`:

```python
import logging
import os
import re
import signal
import subprocess
import sys
import threading
```

- [ ] **Step 4: Replace the reboot block in `_handle_ota`**

In `station_agent/agent.py`, replace the existing lines 145-151 (the `# Report rebooting` block through the inline `self._verify_and_commit(...)` call) with:

```python
        # Report rebooting first so the server knows the station is
        # going down intentionally; a failed report here must not
        # block the reboot itself.
        report_status(config, http_client, result_pk, "rebooting")

        # Real reboot. systemctl queues the reboot with systemd and
        # returns 0 immediately — we are not killed until systemd
        # tears down services (typically a few seconds later). We
        # must NOT return here: the heartbeat loop's next OTA check
        # would hit the post-reboot-recovery path BEFORE the reboot
        # actually happened and verify against the still-running-old
        # rootfs. Block on the shutdown event until systemd signals
        # us. _verify_and_commit then fires on the NEXT boot via the
        # post-reboot-recovery path at the top of _handle_ota
        # (deployment_result_status in {"rebooting", "verifying"}).
        try:
            subprocess.run(["systemctl", "reboot"], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            # No systemctl, permission denied, unit refused, etc. —
            # slot_b is written and armed but the switch didn't
            # happen. Tell the server so the operator sees FAILED.
            logger.error("Reboot failed: %s", exc)
            report_status(
                config,
                http_client,
                result_pk,
                "failed",
                error_message=f"Reboot call failed: {exc}",
            )
            return

        logger.info("Reboot queued — waiting for systemd shutdown signal")
        # Block up to 5 minutes. SIGTERM from systemd sets the
        # shutdown event → wait returns True → return cleanly.
        # Timeout with still-alive process → report FAILED
        # (reboot was queued but inhibited).
        if self._shutdown.wait(timeout=300):
            return

        logger.error("Reboot queued 5 minutes ago but shutdown signal never came")
        report_status(
            config,
            http_client,
            result_pk,
            "failed",
            error_message=(
                "Reboot queued via systemctl but shutdown signal never "
                "arrived within 5 minutes — reboot was likely inhibited."
            ),
        )
```

- [ ] **Step 5: Run the tests to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestHandleOtaRebootTransition -v
```

Expected: 3 passed.

- [ ] **Step 6: Full suite sanity check**

```
cd ~/station-manager && .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: no regressions.

- [ ] **Step 7: Ruff**

```
cd ~/station-manager && .venv/bin/ruff format station_agent/agent.py tests/test_agent_ota_flow.py && \
  .venv/bin/ruff check station_agent/agent.py tests/test_agent_ota_flow.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```
cd ~/station-manager && \
  git add station_agent/agent.py tests/test_agent_ota_flow.py && \
  git commit -m "agent: real systemctl reboot; drop inline verify simulation

_handle_ota used to log 'in production, device would reboot now'
and call _verify_and_commit inline. That meant a station never
actually tried the new slot — bootloader env said boot_part=b but
the kernel kept running from slot_a. The 2026-04-22 e2e test showed
the resulting second-OTA-overwrites-live-slot failure mode.

Replace the simulation with subprocess.run(['systemctl', 'reboot'])
and drop the inline verify call. The existing post-reboot-recovery
path at the top of _handle_ota (deployment_result_status in
{'rebooting', 'verifying'}) is now the sole entrypoint to
_verify_and_commit. Prod and test share one codepath; tests mock
subprocess.run at the boundary.

Reboot-call exceptions (no systemctl, permission denied, unit
refused) and the unlikely 'returned without rebooting' case both
report FAILED so the operator sees a station stuck on
'rebooting->failed' instead of forever on 'rebooting'."
```

---

## Task 4: Filename rename + legacy partial sweep

**Goal:** Rename the local download filename from `firmware-{version}.wic.bz2` to `firmware-{version}.rootfs.bz2`. Remove any legacy `firmware-*.wic.bz2` files on first download pass so a one-time migration doesn't leave orphans in `/tmp/station-agent/`.

**Files:**
- Modify: `station_agent/agent.py`
- Modify: `tests/test_agent_ota_flow.py`

### Steps

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_ota_flow.py`:

```python
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
        monkeypatch.setattr(
            "station_agent.agent.report_status", lambda *a, **kw: None
        )

        def fake_download(**kw):
            captured["dest_path"] = kw["dest_path"]
            return True

        monkeypatch.setattr(
            "station_agent.agent.download_firmware_resumable", fake_download
        )
        monkeypatch.setattr(
            "station_agent.agent.apply_update", lambda cfg, path: True
        )
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )
        monkeypatch.setattr(
            StationAgent,
            "_verify_and_commit",
            lambda *a, **kw: None,
        )

        class _Cfg:
            download_dir = str(download_dir)
            server_url = "http://localhost"

        agent = StationAgent()
        agent._handle_ota(_Cfg(), object())

        assert captured["dest_path"].endswith(".rootfs.bz2")
        assert "wic.bz2" not in captured["dest_path"]

    def test_legacy_wic_partials_are_swept_before_download(
        self, monkeypatch, tmp_path
    ):
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
        monkeypatch.setattr(
            "station_agent.agent.report_status", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "station_agent.agent.download_firmware_resumable", lambda **kw: True
        )
        monkeypatch.setattr(
            "station_agent.agent.apply_update", lambda cfg, path: True
        )
        monkeypatch.setattr(
            "station_agent.agent.subprocess.run",
            lambda argv, **kw: type("CP", (), {"returncode": 0})(),
        )
        monkeypatch.setattr(
            StationAgent,
            "_verify_and_commit",
            lambda *a, **kw: None,
        )

        class _Cfg:
            download_dir = str(download_dir)
            server_url = "http://localhost"

        agent = StationAgent()
        agent._handle_ota(_Cfg(), object())

        assert not legacy.exists(), "legacy .wic.bz2 partial was not removed"
        assert keep.exists(), "unrelated file must stay"
```

- [ ] **Step 2: Run the tests — expect FAIL**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestDestPathAndLegacySweep -v
```

Expected: the "uses_rootfs_suffix" test fails (current code writes `.wic.bz2`), the "legacy_wic_partials" test fails (no sweep logic exists yet).

- [ ] **Step 3: Rename the dest_path template + add legacy sweep**

In `station_agent/agent.py`, find the existing line (around 85):

```python
        dest_path = os.path.join(config.download_dir, f"firmware-{safe_version}.wic.bz2")
```

Replace with:

```python
        dest_path = os.path.join(
            config.download_dir, f"firmware-{safe_version}.rootfs.bz2"
        )

        # One-time sweep: old agents used `.wic.bz2` as the suffix.
        # After the server-side switch to rootfs artifacts (PR #28) any
        # such partial left in download_dir is unreachable (the new
        # filename can't match it) and would accumulate forever. Purge
        # once; steady state has none.
        import glob as _glob

        for stale in _glob.glob(
            os.path.join(config.download_dir, "firmware-*.wic.bz2")
        ):
            try:
                os.remove(stale)
                logger.info("Removed legacy partial: %s", stale)
            except OSError as exc:
                logger.warning(
                    "Could not remove legacy partial %s: %s", stale, exc
                )
```

(The local `import glob as _glob` is intentional — the sweep runs rarely, and keeping the import at the module top would add a symbol to the file's public namespace that nothing else uses. If the reviewer prefers a module-level import, move it to the top alongside `os` and drop the underscore prefix — either is fine.)

- [ ] **Step 4: Run the tests to verify PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_agent_ota_flow.py::TestDestPathAndLegacySweep -v
```

Expected: 2 passed.

- [ ] **Step 5: Full suite + ruff**

```
cd ~/station-manager && \
  .venv/bin/ruff format station_agent/agent.py tests/test_agent_ota_flow.py && \
  .venv/bin/ruff check station_agent/agent.py tests/test_agent_ota_flow.py && \
  .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: clean ruff, all tests pass.

- [ ] **Step 6: Commit**

```
cd ~/station-manager && \
  git add station_agent/agent.py tests/test_agent_ota_flow.py && \
  git commit -m "agent: rename download to .rootfs.bz2; sweep legacy .wic.bz2

Server-side PR #28 switched the OTA download artifact from the full
wic to the extracted root partition. The agent was still writing
it to disk under firmware-{version}.wic.bz2, which was misleading
for anyone inspecting /tmp/station-agent/ contents. Rename to
.rootfs.bz2 to match what's actually there.

A station coming from the previous agent may have a stale
firmware-*.wic.bz2 partial in download_dir that the new filename
will never find (and that download_firmware_resumable's
expected_size check can't discard because it isn't addressable by
name). Sweep them once on first download pass — steady state has
none."
```

---

## Task 5: Push + PR

**Goal:** Push `feat/agent-reboot-and-slot-detection` and open the PR.

### Steps

- [ ] **Step 1: Push the branch**

```
cd ~/station-manager && git push -u origin feat/agent-reboot-and-slot-detection 2>&1 | tail -3
```

- [ ] **Step 2: Open the PR**

```
cd ~/station-manager && gh pr create \
  --title "station-agent: real reboot + runtime slot detection + trial-flag guard" \
  --body "$(cat <<'EOF'
## Summary

Four agent-side OTA fixes, all triggered by the 2026-04-22 e2e test on the qemux86-64 station:

1. \`bootloader.get_active_slot\` is now derived from \`/proc/cmdline\` + root-mount inspection, not the bootloader env. The env's \`boot_part\` is a next-boot hint that \`set_upgrade_pending\` mutates before reboot; reading it let \`install_to_slot\` overwrite the live rootfs on the second consecutive OTA. Fails closed with RuntimeError if neither probe resolves.
2. Real \`systemctl reboot\` after install, replacing the inline \`_verify_and_commit\` simulation. The existing post-reboot-recovery path is the sole verify entrypoint — prod and test share one codepath.
3. \`_verify_and_commit\` rejects commit when \`upgrade_available != "1"\` (bootloader rolled back, or env unreadable). Catches the same-version-redeploy blind spot the \`/etc/os-release\` check can't see.
4. Download filename renamed to \`.rootfs.bz2\` (post-PR #28 server-side change) with a one-time sweep of legacy \`.wic.bz2\` partials.

## Design

- Spec: \`docs/superpowers/specs/2026-04-22-agent-reboot-and-slot-detection-design.md\`
- Plan: \`docs/superpowers/plans/2026-04-22-agent-reboot-and-slot-detection.md\`

Bootloader-agnostic slot detection works because both \`oe5xrx-grub.cfg\` and \`boot.cmd\` set \`root=PARTLABEL=root_\${boot_part}\` in the kernel cmdline. One regex handles both.

## Test plan

- [x] Unit tests for each probe (cmdline / mount fallback / RuntimeError).
- [x] Unit tests for \`apply_update\` catching RuntimeError → False.
- [x] Unit tests for the trial-flag guard (rolled_back on 0, rolled_back on None, commit on 1).
- [x] Unit tests for \`_handle_ota\` invoking systemctl reboot, not calling verify inline, and reporting FAILED on reboot-call errors.
- [x] Unit tests for \`.rootfs.bz2\` filename + legacy \`.wic.bz2\` sweep.
- [ ] After merge: bump \`SRCREV\` in linux-image, cut a new image release, reflash the Proxmox test station, run a same-tag OTA re-deploy and watch the station actually reboot into slot_b and verify from \`/etc/os-release\`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed to stdout.

- [ ] **Step 3: Done**

Await review. If copilot-loop runs, iterate per that flow.

---

## Out of scope (tracked elsewhere)

- **Partition-identity audit log during commit.** The \`upgrade_available\` guard closes the correctness hole; an explicit \`get_active_slot()\` vs "intended target slot" comparison would be nice-to-have for forensics but doesn't close a hole. Follow-up ticket.
- **Dropping the \`bootloader\` parameter from \`get_active_slot\`.** Detection is bootloader-agnostic, so the parameter is ignored. Removing it would ripple through every caller (and test) for no behavioural gain.
- **D-Bus reboot.** \`subprocess.run(["systemctl", "reboot"])\` matches what scripts across the image already do and has zero extra deps.
- **End-to-end test landing into slot_b.** Needs a real or realistic-QEMU environment; covered by manual post-merge rollout step, not by pytest.
