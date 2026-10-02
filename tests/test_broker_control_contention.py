# tests/test_broker_control_contention.py
"""Regression: the FM module flaps offline every re-discovery cycle on a real station.

Live bug (RC#3): PR #135 added a periodic re-discovery loop so a module that lost the
startup race comes back online. But ``control_client._rediscovery_loop`` ran
``discover_slots`` on its own, with no coordination against the broker's per-slot control
lock. Re-discovery probes the *same* single ``ttyACM0`` control serial that telemetry
polls and commands use (``broker._execute``): a re-scan racing an in-flight poll tick on
the wire corrupts the probe's ``MODULE-LIST``/``MODULE-DESCRIBE`` reply, ``probe_slot``
gives up, and the slot drops out of inventory for that cycle. The result the operator
sees is the module "verabschiedet sich" (flaps online/offline) every ~30 s in
station-manager's inventory.

Two coupled defects, one root (the shared, unsynchronised control serial):

1. ``Broker.rediscover`` must run the blocking scan with every known slot's control lock
   held, so discovery is mutually exclusive with per-slot device I/O (this test file's
   ``test_rediscover_serializes_against_slot_io``).
2. When a slot drops from inventory, a telemetry subscription armed while it was present
   keeps polling. ``_execute`` then built ``SlotControl(_control_path(slot) -> None)`` and
   did ``os.open(None)`` — ``TypeError`` once per cap per tick (~1×/s). Physical modules
   got no guard; only virtual ones did (RC#2). Now it fails closed with ``unknown_slot``
   (``test_execute_offline_*`` / ``test_dropped_slot_poll_builds_no_transport``).
"""

import asyncio

import pytest

from station_agent import protocol as proto
from station_agent.broker import Broker


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _run(coro):
    return asyncio.run(coro)


_SLOT2 = {
    "slot": 2,
    "control": "/dev/oe5xrx/slot2/control",
    "modules": [
        {
            "id": "fm",
            "identity": {"type": "fm_transceiver"},
            "capabilities": [
                {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True},
            ],
        }
    ],
}


class _FakeTransport:
    """Records the control path it was built for and returns a canned telemetry value."""

    def __init__(self, path):
        self.path = path

    def execute(self, module_id, op, cap, token=None, trace=False):
        return {"ok": True, "value": 7}


_SLOT3 = {
    "slot": 3,
    "control": "/dev/oe5xrx/slot3/control",
    "modules": [{"id": "pa", "identity": {}, "capabilities": []}],
}


def _phys_broker(transport_factory, inventory=None):
    col = Collector()
    b = Broker(
        col,
        transport_factory=transport_factory,
        telemetry_min_floor_ms=10,
        telemetry_default_interval_ms=20,
        now=lambda: 1.0,
    )
    b.set_inventory([_SLOT2] if inventory is None else inventory)
    return b, col


def test_execute_offline_when_slot_has_no_control_path():
    """A telemetry poll that reaches ``_execute`` after its slot left inventory must fail
    closed with ``unknown_slot`` instead of building ``SlotControl(None)`` and raising
    ``TypeError`` in ``os.open(None)``."""
    b, _ = _phys_broker(lambda path: _FakeTransport(path))
    b.set_inventory([])  # slot 2 dropped: _control_path(2) is now None

    async def scenario():
        return await b._execute(2, "fm", "get", "rssi", None)

    result = _run(scenario())
    assert result == {"ok": False, "error": proto.UNKNOWN_SLOT}, result


def test_dropped_slot_poll_builds_no_transport():
    """End-to-end: subscribe telemetry while the slot is present, then drop the slot. The
    persisted poll keeps ticking but must never hand a ``None`` path to the transport
    factory (which would ``os.open(None)``)."""
    paths = []

    def spy_factory(path):
        paths.append(path)
        return _FakeTransport(path)

    b, _ = _phys_broker(spy_factory)

    async def scenario():
        await b.handle_subscribe(
            {"slot": 2, "module": "fm", "capabilities": ["rssi"], "interval_ms": 10}
        )
        await asyncio.sleep(0.05)  # ticks while present -> factory sees the real path
        b.set_inventory([])  # slot 2 drops; subscription persists
        await asyncio.sleep(0.05)  # ticks while absent -> must NOT see a None path
        await b.stop()

    _run(scenario())
    assert None not in paths, f"transport factory received a None control path: {paths}"
    assert paths, "expected the poll to build a transport while the slot was present"


def test_rediscover_serializes_against_slot_io():
    """``Broker.rediscover`` must hold a slot's control lock for the whole scan, so a probe
    never shares the serial line with an in-flight poll/command on that slot. With the
    lock held by a simulated in-flight access, the blocking scan must not start until the
    lock is released."""
    b, _ = _phys_broker(lambda path: _FakeTransport(path))
    # Simulate the lock an in-flight telemetry poll / command holds for slot 2.
    b._slot_locks[2] = asyncio.Lock()
    ran = []

    def discover_fn():
        ran.append("scanned")
        return [_SLOT2]

    async def scenario():
        lock = b._slot_locks[2]
        await lock.acquire()
        task = asyncio.ensure_future(b.rediscover(discover_fn))
        await asyncio.sleep(0.02)
        assert not ran, "discovery ran on the wire while slot 2's control lock was held"
        lock.release()
        result = await task
        assert ran == ["scanned"], "discovery never ran after the lock was released"
        return result

    assert _run(scenario()) == [_SLOT2]


def test_rediscover_releases_all_locks_when_scan_raises():
    """If the blocking scan raises, rediscover must still release every lock it took — a
    leaked lock would silently deadlock every poll/command on that slot forever
    (_rediscovery_loop swallows the exception and keeps going, so it would never recover)."""
    b, _ = _phys_broker(lambda path: _FakeTransport(path))

    def boom():
        raise RuntimeError("probe blew up")

    async def scenario():
        with pytest.raises(RuntimeError):
            await b.rediscover(boom)
        # The slot lock must be free: a follow-up execute must complete, not hang.
        return await asyncio.wait_for(b._execute(2, "fm", "get", "rssi", None), 0.5)

    assert _run(scenario()) == {"ok": True, "value": 7}


def test_rediscover_acquires_every_slot_lock_in_order():
    """rediscover must hold ALL slots' control locks for the scan, acquired in sorted order.
    With a lower slot free and a higher slot held, it grabs the lower, then blocks on the
    higher — proving multi-slot serialization (the single-slot test can't show ordering)."""
    b, _ = _phys_broker(lambda path: _FakeTransport(path), inventory=[_SLOT2, _SLOT3])
    b._slot_locks[2] = asyncio.Lock()
    b._slot_locks[3] = asyncio.Lock()
    ran = []

    def discover_fn():
        ran.append("scanned")
        return [_SLOT2, _SLOT3]

    async def scenario():
        await b._slot_locks[3].acquire()  # hold the higher slot
        task = asyncio.ensure_future(b.rediscover(discover_fn))
        await asyncio.sleep(0.02)
        assert not ran, "scan ran while slot 3's lock was held"
        assert b._slot_locks[2].locked(), "rediscover must already hold slot 2 while waiting on 3"
        b._slot_locks[3].release()
        result = await task
        assert ran == ["scanned"]
        return result

    assert _run(scenario()) == [_SLOT2, _SLOT3]


def test_command_on_dropped_slot_builds_no_transport():
    """A command for a slot that left inventory must come back as a clean unknown_slot result
    (via handle_command's descriptor check) — never reaching _execute to build SlotControl."""
    paths = []

    def spy_factory(path):
        paths.append(path)
        return _FakeTransport(path)

    b, col = _phys_broker(spy_factory)
    b.set_inventory([])  # slot 2 dropped

    async def scenario():
        await b.handle_command(
            {
                "type": "command",
                "request_id": 1,
                "slot": 2,
                "module": "fm",
                "op": "get",
                "capability": "rssi",
            }
        )

    _run(scenario())
    assert None not in paths, f"transport built for a dropped slot via command path: {paths}"
    results = [m for m in col.sent if m.get("type") == "result"]
    assert results and results[-1].get("ok") is False, f"expected an error result; sent={col.sent}"
