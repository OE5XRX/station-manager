"""Process-wide single-owner lock for a slot's control device node.

A slot's control serial (``/dev/oe5xrx/slotN/control``, real CDC-ACM ``ttyACM*``) is a single
line. Three openers in the station_agent PROCESS reach it: control-plane commands / telemetry
polls (``slot_control.SlotControl.execute``), re-discovery, and the heartbeat's inventory scan
(both via ``slot_discovery``). The broker's per-slot ``asyncio.Lock`` only serializes the first
two — they share the control event loop. The heartbeat runs in a SEPARATE thread, so no asyncio
primitive can serialize it against the control plane; its unsynchronised ``discover_slots`` was
the last opener still able to corrupt an in-flight command/poll on the wire.

Enforcement is a path-keyed, process-wide ``threading.Lock`` acquired BEFORE the device is
opened and held until after it is closed. Opening and configuring a tty (baud, raw mode,
input-buffer reset) and restoring termios / closing it are themselves line-perturbing, so the
lock must span the ENTIRE open→configure→converse→restore→close lifecycle of every opener — an
advisory ``flock`` taken on the already-open fd would leave the open/configure and
restore/close windows unguarded (Copilot review #151, round 2). Cross-process exclusivity
(e.g. a manual ``selftest`` while the agent runs) is covered by operational HW discipline
(single concurrent access), not by this in-process lock.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

logger = logging.getLogger(__name__)

# How long an opener waits for the device lock before failing closed. Comfortably above any
# single holder's worst-case I/O budget (an SA818 AT command ~2 s inside the 5 s control
# timeout), so a normal in-flight command never spuriously times out a waiter; a holder that
# wedged and never releases still cannot block a waiter forever.
DEFAULT_LOCK_TIMEOUT = 15.0

# One lock per control device path, created on first use. Guarded by _registry_guard so two
# threads racing to first-open the same path share a single lock instance.
_registry_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _lock_for(path: str) -> threading.Lock:
    # Canonicalise so a udev symlink and its real ttyACM node map to one lock. realpath never
    # raises (it returns the longest resolvable prefix for a missing/dangling path).
    key = os.path.realpath(path)
    with _registry_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


@contextlib.contextmanager
def control_device_lock(path: str, *, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """Hold the process-wide single-owner lock for *path* across the whole block.

    MUST be entered BEFORE the device is opened and kept until after it is closed, so no other
    opener in the process can open / configure / converse / restore / close the same control
    line concurrently. Raises :class:`TimeoutError` if the lock cannot be acquired within
    *timeout*, so the caller fails closed rather than proceeding as a second concurrent owner.
    """
    lock = _lock_for(path)
    if not lock.acquire(timeout=timeout):
        raise TimeoutError(
            f"control device {path!r}: single-owner lock not acquired within {timeout:.1f}s"
        )
    try:
        yield
    finally:
        lock.release()
