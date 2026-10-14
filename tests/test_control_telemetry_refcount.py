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
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import telemetry_subscriptions as subs
from apps.control.models import TelemetrySubscription
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


def _setup():
    station = Station.objects.create(name="tref", status="online")
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
