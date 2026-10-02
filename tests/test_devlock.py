"""Process-wide control-device lock (Copilot finding #2).

The heartbeat's inventory scan runs in a separate thread and used to call ``discover_slots``
with no coordination against the control plane — a THIRD opener on the same ``ttyACM0`` the
broker's asyncio lock cannot serialize (the asyncio lock only orders accesses inside the
control event loop). ``devlock.control_device_lock`` is the cross-thread/cross-component
enforcement: an OS advisory ``flock`` on the device fd that EVERY opener takes.

These tests exercise the primitive directly (real ``flock`` on a temp file, two threads) and
the two call sites — ``slot_discovery.probe_slot`` and ``slot_control.SlotControl.execute`` —
to prove each opener is serialized behind an external holder of the device lock.
"""

import os
import threading
import time

import pytest

from station_agent import devlock, slot_discovery
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware

FM = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver"},
    "capabilities": [
        {"name": "frequency", "kind": "setting", "type": "float"},
        {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
    ],
}


def test_lock_is_exclusive_and_times_out_while_held(tmp_path):
    """A second acquirer cannot take the lock while the first holds it, and fails closed with
    TimeoutError rather than opening a concurrent owner."""
    path = tmp_path / "control"
    path.write_bytes(b"")
    fd_a = os.open(str(path), os.O_RDWR)
    fd_b = os.open(str(path), os.O_RDWR)
    try:
        with devlock.control_device_lock(fd_a, timeout=1.0):
            with pytest.raises(TimeoutError):
                with devlock.control_device_lock(fd_b, timeout=0.2):
                    pass
        # Released on exit: the second acquirer now succeeds immediately.
        with devlock.control_device_lock(fd_b, timeout=1.0):
            pass
    finally:
        os.close(fd_a)
        os.close(fd_b)


def test_lock_blocks_until_holder_releases(tmp_path):
    """A waiter blocks (does not fail) while the lock is held, then acquires once released —
    the "take turns" behaviour that serializes heartbeat vs. control plane."""
    path = tmp_path / "control"
    path.write_bytes(b"")
    fd_a = os.open(str(path), os.O_RDWR)
    fd_b = os.open(str(path), os.O_RDWR)
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with devlock.control_device_lock(fd_a, timeout=2.0):
            acquired.set()
            release.wait(2.0)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert acquired.wait(2.0)
        got = []

        def waiter():
            with devlock.control_device_lock(fd_b, timeout=2.0):
                got.append(time.monotonic())

        w = threading.Thread(target=waiter)
        w.start()
        time.sleep(0.2)
        assert not got, "waiter acquired the lock while it was still held"
        release.set()
        w.join(2.0)
        assert got, "waiter never acquired the lock after release"
    finally:
        release.set()
        t.join(2.0)
        os.close(fd_a)
        os.close(fd_b)


def _hold_external_lock(control_path, acquired, release):
    """Open the same device node and hold the device lock until *release* is set."""
    fd = os.open(control_path, os.O_RDWR | os.O_NOCTTY)
    try:
        with devlock.control_device_lock(fd, timeout=2.0):
            acquired.set()
            release.wait(5.0)
    finally:
        os.close(fd)


def test_probe_slot_serializes_behind_device_lock():
    """``probe_slot`` must take the device lock: with an external holder, it blocks until the
    lock frees, then succeeds — it never talks to the serial concurrently with the holder."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_external_lock, args=(fw.control_path, acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(2.0)
        result = {}

        def probe():
            result["modules"] = slot_discovery.probe_slot(fw.control_path, timeout=2.0)

        t = threading.Thread(target=probe)
        t.start()
        time.sleep(0.3)
        assert "modules" not in result, "probe_slot ran while the device lock was held"
        release.set()
        t.join(5.0)
        assert result.get("modules"), "probe_slot never completed after the lock released"
        assert result["modules"][0]["id"] == "fm"
    finally:
        release.set()
        holder.join(2.0)
        fw.stop()


def test_slot_control_execute_serializes_behind_device_lock():
    """``SlotControl.execute`` must take the device lock too: with an external holder it blocks
    until the lock frees, then completes the command."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_external_lock, args=(fw.control_path, acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(2.0)
        result = {}

        def run():
            sc = SlotControl(fw.control_path, timeout=2.0)
            result["r"] = sc.execute("fm", "get", "rssi")

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.3)
        assert "r" not in result, "execute ran while the device lock was held"
        release.set()
        t.join(5.0)
        assert result.get("r", {}).get("ok") is True, f"execute failed: {result.get('r')}"
    finally:
        release.set()
        holder.join(2.0)
        fw.stop()
