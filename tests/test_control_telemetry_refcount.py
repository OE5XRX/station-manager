"""Telemetry subscriptions are station-wide at the agent: viewers must not be able
to stop or slow down telemetry another viewer relies on (final review, item 2).

ControlConsumer reference-counts subscriptions per (station, slot, module,
capability) across browser connections and forwards ``unsubscribe`` only when the
last subscriber of a capability leaves. No pytest-asyncio: asyncio.run().
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


def _sub(caps=("rssi",), interval_ms=500, slot=0):
    return {
        "v": V,
        "type": "subscribe",
        "slot": slot,
        "module": "fm0",
        "capabilities": list(caps),
        "interval_ms": interval_ms,
    }


def _unsub(caps=("rssi",), slot=0):
    return {
        "v": V,
        "type": "unsubscribe",
        "slot": slot,
        "module": "fm0",
        "capabilities": list(caps),
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
        assert (await _next(layer, spy))["type"] == "subscribe"
        await b.send_json_to(_sub())
        assert (await _next(layer, spy))["type"] == "subscribe"

        await b.send_json_to(_unsub())
        assert await _nothing(layer, spy)

        await a.send_json_to(_unsub())
        frame = await _next(layer, spy)
        assert frame == _unsub()
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
        await _next(layer, spy)

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
        assert (await _next(layer, spy))["interval_ms"] == 500
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
        await a.send_json_to(_sub(interval_ms=1))
        assert (await _next(layer, spy))["interval_ms"] == subs.INTERVAL_MIN_MS
        await a.disconnect()

    asyncio.run(scenario())


def test_malformed_subscribe_not_relayed():
    station, op, _ = _setup()

    async def scenario():
        layer, spy = await _spy(station)
        a = await _connect(op, station)
        await a.send_json_to({"v": V, "type": "subscribe", "slot": 0, "module": "fm0"})
        await a.send_json_to({**_sub(), "slot": True})
        await a.send_json_to({**_sub(), "module": "x" * 200})
        assert await _nothing(layer, spy)
        await a.disconnect()

    asyncio.run(scenario())
