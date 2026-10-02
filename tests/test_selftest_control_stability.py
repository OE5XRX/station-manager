# tests/test_selftest_control_stability.py
"""Smoke tests for the control-stability selftest's OWN logic (PR #135 flap gate): it must
PASS when re-discovery keeps returning the present slot and FAIL when any scan drops it.

HONESTY: these are hardware-free (injected ``discover_fn`` / ``transport_factory`` / clock),
so they verify the harness wires up (subscribe → scan → count flaps → return the right code)
— they do NOT exercise the real serial contention (no wire, no interleaving). The actual
lock regression guard is ``test_broker_control_contention.test_rediscover_*``; the contention
itself is only proven by ``python -m station_agent selftest control-stability`` on real CM4
(Serial-Boundary honesty rule)."""

from station_agent import selftest

_ENTRY = {
    "slot": 2,
    "control": "/dev/slot2",
    "modules": [
        {
            "id": "fm",
            "identity": {"type": "fm_transceiver"},
            "capabilities": [{"name": "rssi", "kind": "telemetry", "type": "int"}],
        }
    ],
}


class _FakeTransport:
    def __init__(self, path):
        pass

    def execute(self, module_id, op, cap, token=None, trace=False):
        return {"ok": True, "value": 1}


def _fake_clock(step):
    state = {"v": 0.0}

    def now():
        v = state["v"]
        state["v"] += step
        return v

    return now


async def _nosleep(_s):
    return None


def test_stable_rediscovery_passes():
    def discover():
        return [_ENTRY]

    rc = selftest.run_control_stability(
        "/dev/oe5xrx",
        duration=3.0,
        discover_fn=discover,
        transport_factory=_FakeTransport,
        now=_fake_clock(1.0),
        sleep=_nosleep,
    )
    assert rc == 0


def test_flapping_rediscovery_fails():
    calls = {"n": 0}

    def discover():
        calls["n"] += 1
        # call 1 = initial (present, so there is something to test); later scans flap out
        return [_ENTRY] if calls["n"] == 1 else []

    rc = selftest.run_control_stability(
        "/dev/oe5xrx",
        duration=3.0,
        discover_fn=discover,
        transport_factory=_FakeTransport,
        now=_fake_clock(1.0),
        sleep=_nosleep,
    )
    assert rc == 1


def test_single_dropped_scan_fails():
    """Real contention is intermittent — one dropped scan per ~30s. The gate must fail on a
    single flap, not only when every scan drops, so it stays sensitive to the real signature."""
    calls = {"n": 0}

    def discover():
        calls["n"] += 1
        # call 1 = initial (present); drop on exactly ONE later scan, present otherwise
        return [] if calls["n"] == 3 else [_ENTRY]

    rc = selftest.run_control_stability(
        "/dev/oe5xrx",
        duration=5.0,
        discover_fn=discover,
        transport_factory=_FakeTransport,
        now=_fake_clock(1.0),
        sleep=_nosleep,
    )
    assert rc == 1


def test_no_slots_discovered_fails():
    rc = selftest.run_control_stability(
        "/dev/oe5xrx",
        duration=3.0,
        discover_fn=lambda: [],
        transport_factory=_FakeTransport,
        now=_fake_clock(1.0),
        sleep=_nosleep,
    )
    assert rc == 1


def test_non_positive_params_rejected():
    """Copilot finding #3: non-positive CLI params must fail closed (return 1), never run a
    degenerate gate. ``duration<=0`` skips the loop → a false "0 scans, 0 flaps" pass;
    ``poll_hz==0`` → ZeroDivisionError in the interval math; ``rescan_s<=0`` → no pacing.
    ``discover_fn`` is a sentinel that must never be called — validation happens first."""

    def must_not_run():
        raise AssertionError("discover_fn called despite invalid params")

    for bad in (
        {"duration": 0.0},
        {"duration": -1.0},
        {"poll_hz": 0.0},
        {"poll_hz": -5.0},
        {"rescan_s": 0.0},
        {"rescan_s": -2.0},
    ):
        kwargs = {"duration": 3.0, "poll_hz": 10.0, "rescan_s": 2.0}
        kwargs.update(bad)
        rc = selftest.run_control_stability(
            "/dev/oe5xrx",
            discover_fn=must_not_run,
            transport_factory=_FakeTransport,
            now=_fake_clock(1.0),
            sleep=_nosleep,
            **kwargs,
        )
        assert rc == 1, f"expected rc=1 for {bad}, got {rc}"
