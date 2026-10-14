"""Telemetry subscriptions are station-wide at the agent: viewers must not be able
to stop, slow down or flood telemetry another viewer relies on (final review,
item 2 + re-review B1/B2a/B2b).

ControlConsumer reference-counts subscriptions per (station, slot, module,
capability) across browser connections. Only state changes reach the agent:
a capability going 0 -> 1 / 1 -> 0 station-wide, or the effective interval
(fastest subscriber, clamped) changing. Frames are rebuilt from the parsed
address — never relayed verbatim. On agent inventory the aggregated state is
replayed. No pytest-asyncio: asyncio.run().
"""

import asyncio

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import telemetry_subscriptions as subs
from apps.control.models import StationModule, TelemetrySubscription
from apps.stations.models import Station
from config.asgi import application

V = 1
pytestmark = pytest.mark.django_db(transaction=True)


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


def _agent_comm(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _sub(caps=("rssi",), interval_ms=500, slot=0, module="fm0", **extra):
    return {
        "v": V,
        "type": "subscribe",
        "slot": slot,
        "module": module,
        "capabilities": list(caps),
        "interval_ms": interval_ms,
        **extra,
    }


def _unsub(caps=("rssi",), slot=0, module="fm0"):
    return {
        "v": V,
        "type": "unsubscribe",
        "slot": slot,
        "module": module,
        "capabilities": list(caps),
    }


def _expected_sub(caps, interval_ms, slot=0, module="fm0"):
    return {
        "v": V,
        "type": "subscribe",
        "slot": slot,
        "module": module,
        "capabilities": list(caps),
        "interval_ms": interval_ms,
    }


async def _spy(station):
    layer = get_channel_layer()
    name = await layer.new_channel()
    await layer.group_add(f"control_{station.id}_agent", name)
    return layer, name


async def _next(layer, spy):
    return (await asyncio.wait_for(layer.receive(spy), timeout=2.0))["frame"]


async def _nothing(layer, spy):
    try:
        got = await asyncio.wait_for(layer.receive(spy), timeout=0.3)
    except TimeoutError:
        return True
    raise AssertionError(f"unexpected relay: {got}")


#: (slot, module) pairs registered on the test station (subscribe validates them).
_MODULES = ((0, "fm0"), (0, "fm1"), (1, "fm0"), (2, "fm1"))


def _setup():
    station = Station.objects.create(name="tref", status="online")
    for slot, module in _MODULES:
        StationModule.objects.create(station=station, slot=str(slot), module_id=module)
    op = User.objects.create(username="tref-op", membership_level=User.MembershipLevel.MEMBER)
    viewer = User.objects.create(
        username="tref-app", membership_level=User.MembershipLevel.APPLICANT
    )
    return station, op, viewer


async def _connect(user, station):
    comm = _browser(user, station.id)
    assert (await comm.connect())[0] is True
    return comm


def test_viewer_unsubscribe_does_not_stop_operator_telemetry():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub())
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.send_json_to(_sub())
        assert await _nothing(layer, spy)  # no state change -> nothing relayed

        await b.send_json_to(_unsub())
        assert await _nothing(layer, spy)

        await a.send_json_to(_unsub())
        assert await _next(layer, spy) == _unsub()
        await a.disconnect()
        await b.disconnect()

    asyncio.run(scenario())
    assert not TelemetrySubscription.objects.exists()


def test_viewer_disconnect_does_not_stop_operator_telemetry():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(caps=("rssi", "snr")))
        await _next(layer, spy)
        await b.send_json_to(_sub(caps=("rssi",)))

        await b.disconnect()
        assert await _nothing(layer, spy)

        await a.disconnect()
        frame = await _next(layer, spy)
        assert frame["type"] == "unsubscribe"
        assert frame["slot"] == 0 and frame["module"] == "fm0"
        assert sorted(frame["capabilities"]) == ["rssi", "snr"]
        assert frame["v"] == V

    asyncio.run(scenario())
    assert not TelemetrySubscription.objects.exists()


def test_unsubscribe_of_unheld_capability_not_relayed():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub())
        await _next(layer, spy)
        await b.send_json_to(_unsub())  # b never subscribed
        assert await _nothing(layer, spy)
        await a.disconnect()
        await _next(layer, spy)
        await b.disconnect()

    asyncio.run(scenario())


def test_viewer_cannot_slow_down_shared_interval():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(interval_ms=500))
        assert (await _next(layer, spy))["interval_ms"] == 500
        await b.send_json_to(_sub(interval_ms=10**9))
        assert await _nothing(layer, spy)
        await a.disconnect()
        await b.disconnect()

    asyncio.run(scenario())


def test_lone_subscriber_interval_is_clamped():
    station, op, _ = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        await a.send_json_to(_sub(interval_ms=10**9))
        assert (await _next(layer, spy))["interval_ms"] == subs.INTERVAL_MAX_MS
        await a.send_json_to(_sub(caps=("snr",), module="fm1", interval_ms=1))
        assert (await _next(layer, spy))["interval_ms"] == subs.INTERVAL_MIN_MS
        await a.disconnect()

    asyncio.run(scenario())


def test_faster_subscriber_speeds_up_and_leaving_relaxes_interval():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(interval_ms=1000))
        assert (await _next(layer, spy))["interval_ms"] == 1000
        await b.send_json_to(_sub(interval_ms=500))
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.disconnect()  # fastest subscriber leaves -> interval relaxes
        assert await _next(layer, spy) == _expected_sub(["rssi"], 1000)
        await a.disconnect()
        assert (await _next(layer, spy))["type"] == "unsubscribe"

    asyncio.run(scenario())


def test_repeated_identical_subscribes_relay_at_most_once():
    station, _, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        for _ in range(10):
            await b.send_json_to(_sub())
        assert (await _next(layer, spy))["type"] == "subscribe"
        assert await _nothing(layer, spy)
        await b.disconnect()

    asyncio.run(scenario())


def test_toggle_flood_is_throttled(monkeypatch):
    """A lone viewer toggling subscribe/unsubscribe cannot drive unbounded relays."""
    monkeypatch.setattr(subs, "FRAME_BUDGET", 4)
    station, _, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        for _ in range(10):
            await b.send_json_to(_sub())
            await b.send_json_to(_unsub())
        relayed = 0
        while True:
            try:
                await asyncio.wait_for(layer.receive(spy), timeout=0.3)
            except TimeoutError:
                break
            relayed += 1
        assert relayed <= 4
        await b.disconnect()

    asyncio.run(scenario())


def test_frame_budget_window_expires(monkeypatch):
    """The flood guard is a sliding window: once old frames age out, a fresh
    subscribe is processed again (the budget is not a lifetime cap)."""
    monkeypatch.setattr(subs, "FRAME_BUDGET", 2)
    monkeypatch.setattr(subs, "FRAME_WINDOW_SECONDS", 0.2)
    station, _, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        await b.send_json_to(_sub())
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.send_json_to(_unsub())
        assert await _next(layer, spy) == _unsub()
        await b.send_json_to(_sub())  # budget exhausted: dropped
        assert await _nothing(layer, spy)  # also lets the window expire (0.3 s)
        await b.send_json_to(_sub())
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.disconnect()

    asyncio.run(scenario())


def test_malformed_subscribe_not_relayed():
    station, op, _ = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        await a.send_json_to({"v": V, "type": "subscribe", "slot": 0, "module": "fm0"})
        await a.send_json_to({**_sub(), "slot": True})
        await a.send_json_to({**_sub(), "slot": "0"})  # agent accepts int slots only
        await a.send_json_to({**_sub(), "slot": "slot0"})
        await a.send_json_to({**_sub(), "module": "x" * 200})
        await a.send_json_to({**_sub(), "v": 2})
        sub_without_v = _sub()
        del sub_without_v["v"]
        await a.send_json_to(sub_without_v)
        assert await _nothing(layer, spy)
        await a.disconnect()

    asyncio.run(scenario())
    assert not TelemetrySubscription.objects.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"slot": True},
        {"slot": 2**31},
        {"slot": -(2**31)},
        {"module": "x" * 200},
        {"module": ""},
        {"capabilities": [f"c{i}" for i in range(subs._MAX_CAPS + 1)]},
        {"capabilities": [1, None, ["rssi"]]},
        {"capabilities": "rssi"},
        {"capabilities": []},
        {"v": 2},
    ],
)
def test_parse_addr_rejects(overrides):
    assert subs.parse_addr({**_sub(), **overrides}) is None


def test_parse_addr_accepts_and_dedupes_caps():
    assert subs.parse_addr(_sub(caps=("rssi", "rssi", "snr", "rssi"))) == (
        0,
        "fm0",
        ["rssi", "snr"],
    )
    full = [f"c{i}" for i in range(subs._MAX_CAPS)]
    assert subs.parse_addr(_sub(caps=full)) == (0, "fm0", full)
    # non-str entries are skipped, valid ones survive
    assert subs.parse_addr({**_sub(), "capabilities": [1, "rssi"]}) == (0, "fm0", ["rssi"])


def test_duplicate_caps_collapse_to_one_row_and_one_relay():
    station, op, _ = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        await a.send_json_to(_sub(caps=("rssi", "rssi", "rssi")))
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        assert await _nothing(layer, spy)
        assert await sync_to_async(TelemetrySubscription.objects.count)() == 1
        await a.disconnect()

    asyncio.run(scenario())


def test_string_slot_alias_cannot_unsubscribe_operator():
    """Re-review B1: slot "01" (ignored by the agent) must never turn into an
    unsubscribe for slot 1 on disconnect."""
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(slot=1))
        await _next(layer, spy)
        for alias in ("01", "001", "١", " 1"):
            await b.send_json_to(_sub(slot=alias))
        await b.disconnect()
        assert await _nothing(layer, spy)
        await a.disconnect()
        assert await _next(layer, spy) == _unsub(slot=1)

    asyncio.run(scenario())


def test_relayed_frames_carry_only_protocol_keys():
    """Re-review B2b: viewer-chosen keys (op/capability/value) never reach the agent."""
    station, _, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        await b.send_json_to(
            _sub(op="set", capability="frequency", value=1, request_id="x", junk={"a": 1})
        )
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.send_json_to({**_unsub(), "op": "set", "capability": "frequency", "value": 1})
        assert await _next(layer, spy) == _unsub()
        await b.disconnect()

    asyncio.run(scenario())


def test_agent_inventory_replays_aggregated_subscriptions(control_agent_auth):
    """After an agent (re)connect the server replays the current demand; the
    browsers' own re-subscribes are redundant and not relayed."""
    station, op, viewer = _setup()

    async def scenario():
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(caps=("rssi", "snr"), interval_ms=1000))
        await b.send_json_to(_sub(caps=("rssi",), interval_ms=500))
        await b.send_json_to(_sub(caps=("temp",), module="fm1", slot=2, interval_ms=2000))
        await asyncio.sleep(0.2)

        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to({"v": V, "type": "inventory", "slots": []})
        got = [await agent.receive_json_from(timeout=2) for _ in range(2)]
        got = sorted(got, key=lambda f: f["slot"])
        assert got == [
            _expected_sub(["rssi", "snr"], 500),
            _expected_sub(["temp"], 2000, slot=2, module="fm1"),
        ]
        # The browsers re-subscribe on the fresh inventory: no further relay.
        await a.send_json_to(_sub(caps=("rssi", "snr"), interval_ms=1000))
        assert await agent.receive_nothing(timeout=0.3)
        await agent.disconnect()
        await a.disconnect()
        await b.disconnect()

    asyncio.run(scenario())


def test_new_capability_at_unchanged_interval_relays_only_the_new_cap():
    station, op, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        b = await _connect(viewer, station)
        await a.send_json_to(_sub(caps=("rssi",), interval_ms=500))
        assert await _next(layer, spy) == _expected_sub(["rssi"], 500)
        await b.send_json_to(_sub(caps=("snr",), interval_ms=500))
        assert await _next(layer, spy) == _expected_sub(["snr"], 500)
        assert await _nothing(layer, spy)
        await a.disconnect()
        await b.disconnect()

    asyncio.run(scenario())


# -- liveness: rows of connections that died without disconnect() -------------------


def _age(**filters):
    """Backdate rows past the stale threshold (their connection stopped stamping)."""
    from datetime import timedelta

    from django.utils import timezone

    old = timezone.now() - timedelta(seconds=subs.STALE_AFTER_SECONDS + 5)
    TelemetrySubscription.objects.filter(**filters).update(last_seen=old)


def test_stale_rows_are_purged_and_not_replayed_fresh_rows_kept():
    station, _, _ = _setup()
    subs.subscribe(station, "dead-chan", 0, "fm0", ["rssi"], 250)
    subs.subscribe(station, "live-chan", 1, "fm0", ["snr"], 1000)
    _age(channel_name="dead-chan")

    assert subs.active_subscriptions(station) == [_expected_sub(["snr"], 1000, slot=1)]
    assert list(TelemetrySubscription.objects.values_list("channel_name", flat=True)) == [
        "live-chan"
    ]


def test_purge_stale_releases_dead_demand():
    """A dead fastest subscriber no longer pins the interval; a dead lone one is
    unsubscribed."""
    station, _, _ = _setup()
    subs.subscribe(station, "dead-chan", 0, "fm0", ["rssi"], 250)
    subs.subscribe(station, "live-chan", 0, "fm0", ["rssi"], 1000)
    subs.subscribe(station, "dead-chan", 2, "fm1", ["temp"], 500)
    _age(channel_name="dead-chan")

    frames = subs.purge_stale(station)
    assert sorted(frames, key=lambda f: f["slot"]) == [
        _expected_sub(["rssi"], 1000),
        _unsub(caps=("temp",), slot=2, module="fm1"),
    ]
    assert subs.purge_stale(station) == []  # idempotent
    assert not TelemetrySubscription.objects.filter(channel_name="dead-chan").exists()


def test_subscribe_restamps_and_stale_rows_ignored_by_refcount():
    station, _, _ = _setup()
    subs.subscribe(station, "dead-chan", 0, "fm0", ["rssi"], 250)
    _age(channel_name="dead-chan")
    # The live subscriber's demand is computed without the dead row: the dead
    # one is released (unsubscribe), then the live one arms the agent at its interval.
    frames = subs.subscribe(station, "live-chan", 0, "fm0", ["rssi"], 1000)
    assert frames == [_unsub(), _expected_sub(["rssi"], 1000)]
    _age(channel_name="live-chan")
    subs.subscribe(station, "live-chan", 0, "fm1", ["snr"], 1000)  # restamps ALL its rows
    assert subs.purge_stale(station) == []
    assert TelemetrySubscription.objects.filter(channel_name="live-chan").count() == 2


def test_periodic_recheck_restamps_open_connection(monkeypatch):
    from apps.accounts import permissions

    monkeypatch.setattr(permissions, "PERIODIC_RECHECK_SECONDS", 0.05)
    station, _, viewer = _setup()

    async def scenario():
        from asgiref.sync import sync_to_async

        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        await b.send_json_to(_sub())
        await _next(layer, spy)
        await sync_to_async(_age)(module="fm0")
        await asyncio.sleep(0.3)
        assert await sync_to_async(subs.purge_stale)(station) == []
        await b.disconnect()

    asyncio.run(scenario())


def test_failing_liveness_stamp_does_not_close_feed(monkeypatch):
    """touch() is bookkeeping, not a permission check: it failing on every tick
    must not count toward RECHECK_MAX_FAILURES and close the WS."""
    from apps.accounts import permissions

    monkeypatch.setattr(permissions, "PERIODIC_RECHECK_SECONDS", 0.05)
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(subs, "touch", boom)
    station, _, viewer = _setup()

    async def scenario():
        b = await _connect(viewer, station)
        await b.receive_json_from()  # inventory
        await b.receive_json_from()  # lock
        # Many ticks, well past RECHECK_MAX_FAILURES: no error frame, no close.
        assert await b.receive_nothing(timeout=0.4)
        assert len(calls) > permissions.RECHECK_MAX_FAILURES
        # Still a working feed (subscribe() itself re-stamps, so probe via the lock).
        await b.send_json_to({"type": "lock_release"})
        assert (await b.receive_json_from())["type"] == "lock"
        await b.disconnect()

    asyncio.run(scenario())


def test_agent_sweep_relays_purge_of_dead_connection(monkeypatch, control_agent_auth):
    from apps.control import constants

    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station, _, _ = _setup()
    subs.subscribe(station, "dead-chan", 0, "fm0", ["rssi"], 250)
    _age(channel_name="dead-chan")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        assert await agent.receive_json_from(timeout=2) == _unsub()
        await agent.disconnect()

    asyncio.run(scenario())
    assert not TelemetrySubscription.objects.exists()


# -- only registered addresses / described capabilities, bounded per connection ------


def test_unregistered_module_not_recorded_or_relayed():
    station, _, viewer = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        b = await _connect(viewer, station)
        await b.send_json_to(_sub(module="ghost"))
        await b.send_json_to(_sub(slot=7))
        assert await _nothing(layer, spy)
        await b.disconnect()

    asyncio.run(scenario())
    assert not TelemetrySubscription.objects.exists()


def test_capabilities_narrowed_to_module_descriptor():
    station, _, _ = _setup()
    StationModule.objects.filter(station=station, slot="0", module_id="fm0").update(
        capability_descriptor=[
            {"name": "rssi", "kind": "telemetry"},
            {"name": "snr", "kind": "telemetry"},
        ]
    )
    assert subs.subscribe(station, "c1", 0, "fm0", ["bogus", "rssi"], 500) == [
        _expected_sub(["rssi"], 500)
    ]
    assert subs.subscribe(station, "c1", 0, "fm0", ["bogus", "x"], 500) == []
    assert sorted(TelemetrySubscription.objects.values_list("capability", flat=True)) == ["rssi"]


def test_rows_per_connection_are_capped(monkeypatch):
    monkeypatch.setattr(subs, "MAX_ROWS_PER_CHANNEL", 3)
    station, _, _ = _setup()
    frames = subs.subscribe(station, "c1", 0, "fm0", ["a", "b", "c", "d", "e"], 500)
    assert frames == [_expected_sub(["a", "b", "c"], 500)]
    assert subs.subscribe(station, "c1", 0, "fm1", ["z"], 500) == []
    assert TelemetrySubscription.objects.filter(channel_name="c1").count() == 3
    # Another connection has its own budget.
    assert subs.subscribe(station, "c2", 0, "fm1", ["z"], 500) == [
        _expected_sub(["z"], 500, module="fm1")
    ]


def test_stale_threshold_covers_several_recheck_ticks():
    from apps.accounts import permissions

    assert subs.STALE_AFTER_SECONDS >= 3 * permissions.PERIODIC_RECHECK_SECONDS
