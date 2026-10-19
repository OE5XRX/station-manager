import threading

import pytest

from station_agent import slot_pause


@pytest.fixture(autouse=True)
def _reset():
    slot_pause._reset_for_tests()
    yield
    slot_pause._reset_for_tests()


SNAP = {"slot": 1, "control": "/dev/x", "modules": [{"module_type": "fm", "version": "1.0"}]}


def test_pause_resume_is_paused():
    assert not slot_pause.is_paused(1)
    slot_pause.pause(1, SNAP)
    assert slot_pause.is_paused(1)
    assert slot_pause.paused_slots() == frozenset({1})
    slot_pause.resume(1)
    assert not slot_pause.is_paused(1)
    assert slot_pause.paused_slots() == frozenset()


def test_resume_unknown_is_noop():
    slot_pause.resume(9)


def test_updating_entry_none_when_not_paused_or_no_snapshot():
    assert slot_pause.updating_entry(1) is None
    slot_pause.pause(2, None)
    assert slot_pause.is_paused(2)
    assert slot_pause.updating_entry(2) is None


def test_updating_entry_is_deep_copy_with_status():
    slot_pause.pause(1, SNAP)
    entry = slot_pause.updating_entry(1)
    assert entry["modules"][0]["status"] == "updating"
    entry["modules"][0]["version"] = "MUTATED"
    entry["modules"].append({"x": 1})
    again = slot_pause.updating_entry(1)
    assert again["modules"][0]["version"] == "1.0"
    assert len(again["modules"]) == 1
    # caller's original snapshot is not mutated either
    assert "status" not in SNAP["modules"][0]


def test_threaded_hammering():
    errors = []

    def work(n):
        try:
            for _ in range(500):
                slot_pause.pause(n, SNAP)
                slot_pause.is_paused(n)
                slot_pause.paused_slots()
                slot_pause.updating_entry(n)
                slot_pause.resume(n)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert slot_pause.paused_slots() == frozenset()
