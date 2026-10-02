# tests/test_inventory_debounce.py
"""Debounce/hysteresis for slot re-discovery: a *transient* probe miss must not drop a
previously-present module from inventory.

``discover_slots`` omits a slot both when the module is genuinely gone AND when a single
probe fails (a corrupted MODULE-LIST reply, a chatty console, a one-off timeout). Treating
the two the same makes a flaky read flap the module online/offline every re-scan — exactly
the operator-visible symptom. The debouncer keeps a missing-but-recently-present slot in
inventory for up to ``max_misses`` consecutive misses, then drops it, so genuine removal
still surfaces (after a bounded delay) while a transient miss never flaps.
"""

from station_agent.control_client import _SlotInventoryDebouncer

_A = {"slot": 2, "control": "/dev/slot2", "modules": [{"id": "fm"}]}
_A2 = {"slot": 2, "control": "/dev/slot2", "modules": [{"id": "fm"}, {"id": "x"}]}
_B = {"slot": 3, "control": "/dev/slot3", "modules": [{"id": "pa"}]}


def test_present_passes_through():
    d = _SlotInventoryDebouncer(max_misses=2)
    assert d.update([_A]) == [_A]


def test_transient_miss_retains_then_drops():
    d = _SlotInventoryDebouncer(max_misses=2)
    assert d.update([_A]) == [_A]
    assert d.update([]) == [_A], "1st miss must retain last-known-good"
    assert d.update([]) == [_A], "2nd miss (== max) must still retain"
    assert d.update([]) == [], "3rd consecutive miss (> max) must drop"


def test_reappearance_resets_miss_counter():
    d = _SlotInventoryDebouncer(max_misses=2)
    d.update([_A])
    d.update([])  # miss 1
    assert d.update([_A]) == [_A], "reappearance keeps it present"
    # counter reset: it must again tolerate two full misses before dropping
    assert d.update([]) == [_A]
    assert d.update([]) == [_A]
    assert d.update([]) == []


def test_content_change_while_present_passes_through():
    d = _SlotInventoryDebouncer(max_misses=2)
    d.update([_A])
    assert d.update([_A2]) == [_A2], "a present slot's new descriptor must flow through"


def test_content_change_after_intervening_miss_passes_through():
    """The live sequence present -> transient miss (retained) -> reappears with a CHANGED
    descriptor must surface the fresh entry, not the stale retained one."""
    d = _SlotInventoryDebouncer(max_misses=2)
    d.update([_A])
    assert d.update([]) == [_A], "retained during the miss"
    assert d.update([_A2]) == [_A2], "reappearance with new content must flow through"


def test_slots_debounce_independently():
    d = _SlotInventoryDebouncer(max_misses=1)
    assert d.update([_A, _B]) == [_A, _B]
    # slot 3 drops out, slot 2 stays present: slot 2 resets, slot 3 within grace
    assert d.update([_A]) == [_A, _B], "slot 3 retained within grace while slot 2 present"
    assert d.update([_A]) == [_A], "slot 3 exceeds grace and drops; slot 2 unaffected"


def test_max_misses_zero_drops_immediately():
    d = _SlotInventoryDebouncer(max_misses=0)
    d.update([_A])
    assert d.update([]) == [], "max_misses=0 means no grace — legacy drop-on-first-miss"


def test_output_sorted_by_slot():
    d = _SlotInventoryDebouncer(max_misses=2)
    assert d.update([_B, _A]) == [_A, _B], "effective inventory is deterministic (sorted by slot)"
