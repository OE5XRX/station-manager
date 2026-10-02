"""Process-wide advisory lock for a slot's control device node.

A slot's control serial (``/dev/oe5xrx/slotN/control``) is a single-owner line (see
CLAUDE.md "Control-Device Single-Owner-Invariante"). Three independent openers reach it:
control-plane commands / telemetry polls (``slot_control.SlotControl``), re-discovery, and
the heartbeat's inventory scan (both via ``slot_discovery``). The broker's per-slot
``asyncio.Lock`` only serializes the first two — they share the control event loop. The
heartbeat runs in a SEPARATE thread (the ``agent`` main loop), so no asyncio primitive can
serialize it against the control plane; its unsynchronised ``discover_slots`` was the last
opener still able to corrupt an in-flight command/poll on the wire.

An OS advisory lock (``flock``) on the device fd, taken by EVERY opener for the duration of
its conversation, enforces the single-owner invariant across threads and components. flock
is tied to the open file description and released on close, so it also serializes two opens
from the same process. The broker's asyncio lock stays as an intra-loop optimisation.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import time

logger = logging.getLogger(__name__)

# How long an opener waits for the device lock before failing closed. Comfortably above any
# single holder's worst-case I/O budget (an SA818 AT command ~2 s inside the 5 s control
# timeout), so a normal in-flight command never spuriously times out a waiter; a holder that
# wedged and never releases still cannot block a waiter forever.
DEFAULT_LOCK_TIMEOUT = 15.0

_POLL_INTERVAL = 0.05


@contextlib.contextmanager
def control_device_lock(fd: int, *, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """Hold an exclusive advisory (``flock``) lock on *fd* for the duration of the block.

    Polls for the lock up to *timeout* seconds. Raises :class:`TimeoutError` if it cannot be
    acquired in time, so the caller fails closed rather than opening a second concurrent
    owner on the serial line. Released on block exit (and implicitly when *fd* is closed).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"control device fd {fd}: flock not acquired within {timeout:.1f}s"
                ) from exc
            time.sleep(min(_POLL_INTERVAL, remaining))
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
