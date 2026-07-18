"""Channels tests for the control consumers. No pytest-asyncio — async
scenarios run via asyncio.run() inside @pytest.mark.django_db(transaction=True)."""

import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application

V = 1


def _agent_comm(station_id):
    # Ed25519 verification is bypassed in tests by monkeypatching _verify_agent
    # (see conftest fixture control_agent_auth). Path still must match routing.
    return WebsocketCommunicator(application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0")


@pytest.mark.django_db(transaction=True)
def test_agent_inventory_updates_registry_and_broadcasts(control_agent_auth):
    from apps.control.models import StationModule

    station = Station.objects.create(name="ac1", status="online")

    async def scenario():
        layer = get_channel_layer()
        viewer_spy = "viewer-spy-1"
        await layer.group_add(f"control_{station.id}", viewer_spy)

        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True

        await agent.send_json_to(
            {
                "v": V,
                "type": "inventory",
                "slots": [
                    {
                        "slot": "slot0",
                        "modules": [
                            {
                                "module": "fm0",
                                "identity": {"type": "fm", "model": "SA818", "version": "1"},
                                "capabilities": [
                                    {"name": "frequency", "kind": "setting", "type": "float"}
                                ],
                                "state": {"frequency": 145.5},
                            }
                        ],
                    }
                ],
            }
        )

        # Viewer group sees an inventory broadcast.
        evt = await layer.receive(viewer_spy)
        assert evt["type"] == "control.inventory"

        await agent.disconnect()

    asyncio.run(scenario())

    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    # Module is offline after agent disconnect (mark_station_offline runs in disconnect()).
    # The important checks are: the module was created and last_state was persisted.
    assert m.last_state == {"frequency": 145.5}


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_marks_offline_and_frees_lock(control_agent_auth):
    from apps.control import lock
    from apps.control.models import StationModule

    station = Station.objects.create(name="ac2", status="online")
    holder = User.objects.create(username="h2", membership_level=User.MembershipLevel.MEMBER)
    StationModule.objects.create(station=station, slot="slot0", module_id="fm0", online=True)
    lock.acquire(station, holder)

    async def scenario():
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True
        await agent.disconnect()

    asyncio.run(scenario())

    assert StationModule.objects.filter(station=station, online=True).count() == 0
    lk = lock.get_or_create_lock(station)
    assert lk.holder_id is None  # freed on agent disconnect
