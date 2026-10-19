"""Process-wide registry of slots paused for a module firmware update.

While a slot is paused, telemetry polling, rediscovery and inventory scans
must not open its control device, and the inventory reports the last-known
entry with ``"status": "updating"``.
"""

import copy
import threading

_lock = threading.Lock()
_paused: dict[int, dict | None] = {}


def pause(slot: int, snapshot: dict | None) -> None:
    """Pause ``slot``, remembering its last-known inventory entry."""
    with _lock:
        _paused[slot] = copy.deepcopy(snapshot)


def resume(slot: int) -> None:
    with _lock:
        _paused.pop(slot, None)


def is_paused(slot: int) -> bool:
    with _lock:
        return slot in _paused


def paused_slots() -> frozenset[int]:
    with _lock:
        return frozenset(_paused)


class _LivePaused:
    """Container view whose ``in`` checks the CURRENT pause state (not a snapshot).

    Passed as ``skip=`` to ``discover_slots`` so a slot paused mid-scan is not probed
    afterwards."""

    def __contains__(self, slot) -> bool:
        return is_paused(slot)


LIVE_PAUSED = _LivePaused()


def updating_entry(slot: int) -> dict | None:
    """Deep copy of the snapshot with ``status: updating`` on each module."""
    with _lock:
        snap = _paused.get(slot)
        entry = copy.deepcopy(snap) if snap is not None else None
    if entry is None:
        return None
    for module in entry.get("modules", []):
        module["status"] = "updating"
    return entry


def _reset_for_tests() -> None:
    with _lock:
        _paused.clear()
