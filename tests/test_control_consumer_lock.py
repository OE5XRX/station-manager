# tests/test_control_consumer_lock.py
import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application


def _browser_comm(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _drain_until(comm, msg_type, tries=6):
    """Read frames until one of msg_type arrives (skips initial snapshot/lock)."""
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"never saw {msg_type}")


@pytest.mark.django_db(transaction=True)
def test_applicant_rejected():
    station = Station.objects.create(name="cc1", status="online")
    user = User.objects.create(username="app1", membership_level=User.MembershipLevel.APPLICANT)

    async def scenario():
        comm = _browser_comm(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        msg = await comm.receive_json_from()
        assert msg["type"] == "error"
        assert msg["code"] == 4403
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_acquire_broadcasts_lock_and_non_holder_command_rejected():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cc2", status="online")
    holder = User.objects.create(username="h", membership_level=User.MembershipLevel.MEMBER)
    other = User.objects.create(username="o", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-cc2"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        hc = _browser_comm(holder, station.id)
        oc = _browser_comm(other, station.id)
        assert (await hc.connect())[0] is True
        assert (await oc.connect())[0] is True

        # Consume the initial free-state lock frame so the next drain gets the
        # post-acquire held-state (initial + updates share {type:"lock"}).
        init_lock = await _drain_until(hc, "lock")
        assert init_lock["state"] == "free"
        assert init_lock["you_hold"] is False

        await hc.send_json_to({"type": "lock_acquire"})
        lock_evt = await _drain_until(hc, "lock")
        assert lock_evt["state"] == "held"
        assert lock_evt["you_hold"] is True

        # Non-holder command is rejected, never reaches the agent group.
        await oc.send_json_to(
            {"type": "command", "request_id": "r1", "slot": "slot0",
             "module": "fm0", "capability": "frequency", "op": "set", "value": 145.5}
        )
        err = await _drain_until(oc, "error")
        assert err["error"]["code"] == "not_locked"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(agent_spy), timeout=0.3)

        await hc.disconnect()
        await oc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_holder_command_relayed_to_agent():
    station = Station.objects.create(name="cc3", status="online")
    holder = User.objects.create(username="h3", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-cc3"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        hc = _browser_comm(holder, station.id)
        assert (await hc.connect())[0] is True
        # Drain the initial free-state lock frame, then acquire and drain again.
        init_lock = await _drain_until(hc, "lock")
        assert init_lock["state"] == "free"
        await hc.send_json_to({"type": "lock_acquire"})
        await _drain_until(hc, "lock")

        frame = {"type": "command", "request_id": "r9", "slot": "slot0",
                 "module": "fm0", "capability": "frequency", "op": "set", "value": 145.5}
        await hc.send_json_to(frame)

        relayed = await layer.receive(agent_spy)
        assert relayed["type"] == "control.to_agent"
        assert relayed["frame"]["request_id"] == "r9"
        assert relayed["frame"]["value"] == 145.5

        await hc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_admin_preempt_takes_lock():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cc4", status="online")
    holder = User.objects.create(username="h4", membership_level=User.MembershipLevel.MEMBER)
    admin = User.objects.create(username="admin4", membership_level=User.MembershipLevel.ADMIN)
    lockmod.acquire(station, holder)

    async def scenario():
        ac = _browser_comm(admin, station.id)
        assert (await ac.connect())[0] is True
        # Initial lock is already held by `holder`; drain it before preempt so
        # the next drain gets the post-preempt held-state (holder == admin).
        init_lock = await _drain_until(ac, "lock")
        assert init_lock["state"] == "held"
        assert init_lock["holder_id"] == holder.id
        assert init_lock["you_hold"] is False
        await ac.send_json_to({"type": "lock_preempt"})
        evt = await _drain_until(ac, "lock")
        assert evt["state"] == "held"
        assert evt["holder_id"] == admin.id
        await ac.disconnect()

    asyncio.run(scenario())
