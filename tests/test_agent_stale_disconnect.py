"""Stale agent-connection disconnects must not tear down the live connection's state.

Prod pattern (Cloudflare tunnel): the agent's WS dies with 1006, the agent reconnects
within ~2 s and sends inventory — but the server only notices the OLD socket is gone
~28 s later. That late ``disconnect()`` used to mark every module offline, broadcast
``agent_offline``, free the TX lock and clear PTT of the NEW session. Only the current
agent connection per station may run that teardown.

No pytest-asyncio — async scenarios run via asyncio.run() inside
@pytest.mark.django_db(transaction=True), mirroring test_control_consumer_relay.py.
"""

import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application

V = 1

INVENTORY = {
    "v": V,
    "type": "inventory",
    "slots": [
        {
            "slot": "slot0",
            "modules": [
                {
                    "module": "fm0",
                    "identity": {"type": "fm", "model": "SA818", "version": "1"},
                    "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
                    "state": {"frequency": 145.5},
                }
            ],
        }
    ],
}


def _comm(kind, station_id):
    # Ed25519 verification is bypassed by the *_agent_auth fixtures.
    return WebsocketCommunicator(
        application, f"/ws/agent/{kind}/{station_id}/?signature=x&timestamp=0"
    )


async def _drain_types(layer, channel, timeout=0.3):
    """Every event type currently queued for ``channel`` (until the queue is quiet)."""
    types = []
    while True:
        try:
            evt = await asyncio.wait_for(layer.receive(channel), timeout=timeout)
        except TimeoutError:
            return types
        types.append(evt["type"])


@pytest.fixture
def terminal_agent_auth(monkeypatch):
    from apps.tunnel import consumers

    async def _ok(self, station, params):
        return True

    monkeypatch.setattr(consumers.AgentTerminalConsumer, "_verify_agent", _ok)


def _station_with_held_lock_and_ptt(name):
    from apps.audio import gate as audio_gate
    from apps.control import lock

    station = Station.objects.create(name=name, status="online")
    holder = User.objects.create(
        username=f"h-{name}", membership_level=User.MembershipLevel.MEMBER
    )
    lock.acquire(station, holder)
    audio_gate.set_ptt(station, slot=0, module="fm0", ttl=600)
    return station, holder


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_control_stale_disconnect_after_reconnect_keeps_live_state(control_agent_auth):
    from apps.audio.models import AudioGate
    from apps.control import lock
    from apps.control.models import StationModule

    station, holder = _station_with_held_lock_and_ptt("stale-c1")

    async def scenario():
        layer = get_channel_layer()
        spy = "viewer-spy-stale-c1"
        await layer.group_add(f"control_{station.id}", spy)

        old = _comm("control", station.id)
        assert (await old.connect())[0] is True
        new = _comm("control", station.id)
        assert (await new.connect())[0] is True
        await new.send_json_to(INVENTORY)
        assert "control.inventory" in await _drain_types(layer, spy)

        # The server notices the old socket is dead only now.
        await old.disconnect()
        assert "control.agent_offline" not in await _drain_types(layer, spy)
        assert "control.lock" not in await _drain_types(layer, spy, timeout=0.05)

        def _live_state():
            return (
                StationModule.objects.get(station=station, module_id="fm0").online,
                lock.get_or_create_lock(station).holder_id,
                AudioGate.objects.get(station=station).ptt_active,
            )

        from channels.db import database_sync_to_async

        assert await database_sync_to_async(_live_state)() == (True, holder.pk, True)

        # The live connection going away is still the fail-safe teardown.
        await new.disconnect()
        types = await _drain_types(layer, spy)
        assert "control.agent_offline" in types
        assert "control.lock" in types
        assert await database_sync_to_async(_live_state)() == (False, None, False)

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_control_single_connection_disconnect_still_tears_down(control_agent_auth):
    from apps.audio.models import AudioGate
    from apps.control import lock
    from apps.control.models import StationModule

    station, _holder = _station_with_held_lock_and_ptt("stale-c2")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)

    async def scenario():
        agent = _comm("control", station.id)
        assert (await agent.connect())[0] is True
        await agent.disconnect()

    asyncio.run(scenario())

    assert not StationModule.objects.get(station=station, module_id="fm0").online
    assert lock.get_or_create_lock(station).holder_id is None
    assert AudioGate.objects.get(station=station).ptt_active is False


@pytest.mark.django_db(transaction=True)
def test_control_stale_disconnect_before_new_inventory_is_ignored(control_agent_auth):
    """Old disconnect lands between the new connect and its inventory: still stale."""
    from apps.control.models import StationModule

    station = Station.objects.create(name="stale-c3", status="online")
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)

    async def scenario():
        old = _comm("control", station.id)
        assert (await old.connect())[0] is True
        new = _comm("control", station.id)
        assert (await new.connect())[0] is True
        await old.disconnect()
        await new.disconnect()

    asyncio.run(scenario())

    # Only the second (current) disconnect marked offline; a reconnect after it
    # must be able to claim the station again from a clean slate.
    assert not StationModule.objects.get(station=station, module_id="fm0").online
    from apps.control.models import AgentConnection

    assert not AgentConnection.objects.filter(station=station).exists()


@pytest.mark.django_db(transaction=True)
def test_control_rejected_handshake_does_not_tear_down(monkeypatch):
    """A connection that never authenticated is never the current one."""
    from apps.audio.models import AudioGate
    from apps.control import consumers, lock

    async def _deny(self, station, params):
        return False

    monkeypatch.setattr(consumers.AgentControlConsumer, "_verify_agent", _deny)
    station, holder = _station_with_held_lock_and_ptt("stale-c4")

    async def scenario():
        bad = _comm("control", station.id)
        connected, _ = await bad.connect()
        assert connected is False

    asyncio.run(scenario())

    assert lock.get_or_create_lock(station).holder_id == holder.pk
    assert AudioGate.objects.get(station=station).ptt_active is True


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_audio_stale_disconnect_after_reconnect_keeps_ptt(audio_agent_auth):
    from channels.db import database_sync_to_async

    from apps.audio import constants as audio_constants
    from apps.audio.models import AudioGate

    station = Station.objects.create(name="stale-a1", status="online")
    from apps.audio import gate as audio_gate

    audio_gate.set_ptt(station, slot=0, module="fm0", ttl=600)

    def _ptt():
        return AudioGate.objects.get(station=station).ptt_active

    async def scenario():
        layer = get_channel_layer()
        spy = "browser-spy-stale-a1"
        await layer.group_add(audio_constants.browser_group(station.id), spy)

        old = _comm("audio", station.id)
        assert (await old.connect())[0] is True
        await old.send_json_to(
            {"v": V, "type": "advertise", "streams": [{"stream_id": "slot0.rx", "stream_ref": 1}]}
        )
        new = _comm("audio", station.id)
        assert (await new.connect())[0] is True
        await _drain_types(layer, spy)

        await old.disconnect()
        types = await _drain_types(layer, spy)
        assert "audio.gate" not in types
        assert "audio.stream_state" not in types
        assert await database_sync_to_async(_ptt)() is True

        await new.disconnect()
        assert "audio.gate" in await _drain_types(layer, spy)
        assert await database_sync_to_async(_ptt)() is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_terminal_stale_disconnect_does_not_close_browser_sessions(terminal_agent_auth):
    station = Station.objects.create(name="stale-t1", status="online")

    async def scenario():
        layer = get_channel_layer()
        spy = "browser-spy-stale-t1"
        await layer.group_add(f"terminal_{station.id}", spy)

        old = _comm("terminal", station.id)
        assert (await old.connect())[0] is True
        new = _comm("terminal", station.id)
        assert (await new.connect())[0] is True

        await old.disconnect()
        assert "terminal_closed" not in await _drain_types(layer, spy)

        await new.disconnect()
        assert "terminal_closed" in await _drain_types(layer, spy)

    asyncio.run(scenario())
