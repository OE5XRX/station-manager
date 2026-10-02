"""Process-wide single-owner control-device lock (Copilot findings #2 and round-2 follow-ups).

Finding #2: the heartbeat's inventory scan runs in a separate thread and used to call
``discover_slots`` with no coordination against the control plane — a THIRD opener on the same
``ttyACM0`` the broker's asyncio lock (control event loop only) cannot serialize.

Round-2 refinement: an advisory ``flock`` taken on the *already-open* fd is not enough — in
pyserial, opening and configuring the tty (baud, raw mode, input-buffer reset) and restoring
termios / closing it are themselves line-perturbing, so a second opener could disturb an active
exchange during those windows before it ever blocks on the lock. The lock must therefore span
the ENTIRE open→configure→converse→restore→close lifecycle, which means it must be a path-keyed
lock acquired BEFORE the device is opened. ``devlock.control_device_lock`` is that primitive; a
process-wide ``threading.Lock`` keyed by the device path, taken by EVERY opener
(``slot_control.SlotControl.execute`` and ``slot_discovery.probe_slot``).

These tests exercise the primitive directly (two threads, blocking + timeout) and the two call
sites — proving each opener takes the lock BEFORE it opens the serial and is serialized behind
an external holder of the same path lock.
"""

import threading
import time

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
    """A second acquirer cannot take the lock for the same path while the first holds it, and
    fails closed with TimeoutError rather than proceeding as a concurrent owner."""
    path = str(tmp_path / "control")
    with devlock.control_device_lock(path, timeout=1.0):
        done = []

        def second():
            try:
                with devlock.control_device_lock(path, timeout=0.2):
                    done.append("acquired")
            except TimeoutError:
                done.append("timeout")

        t = threading.Thread(target=second)
        t.start()
        t.join(2.0)
        assert done == ["timeout"], done
    # Released on exit: the second acquirer now succeeds immediately.
    with devlock.control_device_lock(path, timeout=1.0):
        pass


def test_lock_blocks_until_holder_releases(tmp_path):
    """A waiter blocks (does not fail) while the lock is held, then acquires once released —
    the "take turns" behaviour that serializes heartbeat vs. control plane."""
    path = str(tmp_path / "control")
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with devlock.control_device_lock(path, timeout=2.0):
            acquired.set()
            release.wait(2.0)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert acquired.wait(2.0)
        got = []

        def waiter():
            with devlock.control_device_lock(path, timeout=2.0):
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


def test_different_paths_do_not_contend(tmp_path):
    """The lock is per-path: two different control nodes never block each other."""
    p1 = str(tmp_path / "a")
    p2 = str(tmp_path / "b")
    with devlock.control_device_lock(p1, timeout=1.0):
        with devlock.control_device_lock(p2, timeout=0.5):
            pass  # must not block


def test_probe_slot_takes_lock_before_opening_the_serial(monkeypatch):
    """Round-2 regression: ``probe_slot`` must acquire the device lock BEFORE it opens the
    serial, so an opener holding the lock blocks the probe at the lock — the serial is never
    even opened (let alone configured) while another owner is on the line."""
    path = "/dev/oe5xrx/slot9/control"
    opened = []

    def spy_serial(*a, **k):
        opened.append(True)
        # Raise the pyserial error probe_slot already handles, so the probe thread exits
        # cleanly (return None) once it legitimately opens after release — the regression is
        # captured by asserting ``opened`` stays empty while the lock is held, below.
        raise slot_discovery.serial.SerialException("spy: no real device")

    monkeypatch.setattr(slot_discovery.serial, "Serial", spy_serial)

    release = threading.Event()
    holder_ready = threading.Event()

    def holder():
        with devlock.control_device_lock(path, timeout=2.0):
            holder_ready.set()
            release.wait(2.0)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert holder_ready.wait(2.0)
        result = {}

        def probe():
            result["r"] = slot_discovery.probe_slot(path, timeout=0.5)

        p = threading.Thread(target=probe)
        p.start()
        time.sleep(0.3)
        assert not opened, "probe_slot opened the serial while the lock was held"
        release.set()
        p.join(2.0)
    finally:
        release.set()
        t.join(2.0)


def _hold_path_lock(path, acquired, release):
    with devlock.control_device_lock(path, timeout=2.0):
        acquired.set()
        release.wait(5.0)


def test_probe_slot_serializes_behind_device_lock():
    """``probe_slot`` must take the device lock: with an external holder, it blocks until the
    lock frees, then succeeds — it never talks to the serial concurrently with the holder."""
    fw = FakeFirmware({"fm": FM})
    fw.start()
    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_path_lock, args=(fw.control_path, acquired, release))
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
    holder = threading.Thread(target=_hold_path_lock, args=(fw.control_path, acquired, release))
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
