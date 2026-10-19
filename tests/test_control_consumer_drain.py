# tests/test_control_consumer_drain.py
"""ControlConsumer surfaces module-firmware drain (Teilbereich D, Task S4)."""

import asyncio
from datetime import UTC, datetime
from unittest import mock

import pytest
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.module_firmware.drain import DrainStatus
from apps.stations.models import Station
from config.asgi import application

DRAINING = "apps.module_firmware.drain.is_draining"
DRAIN_STATUS = "apps.module_firmware.drain.drain_status"
SINCE = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
ACTIVE = DrainStatus(active=True, since=SINCE, module_uids=("uid-1",))
INACTIVE = DrainStatus(active=False, since=None, module_uids=())


def _browser_comm(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _drain_until(comm, msg_type, tries=6):
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"never saw {msg_type}")


def _member(name):
    return User.objects.create(username=name, membership_level=User.MembershipLevel.MEMBER)


@pytest.mark.django_db(transaction=True)
def test_acquire_while_draining_sends_firmware_update_pending_error():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cdr1", status="online")
    user = _member("cdr1u")

    async def scenario():
        with (
            mock.patch(DRAINING, return_value=True),
            mock.patch(DRAIN_STATUS, return_value=ACTIVE),
        ):
            comm = _browser_comm(user, station.id)
            assert (await comm.connect())[0] is True
            await _drain_until(comm, "lock")
            await comm.send_json_to({"type": "lock_acquire"})
            err = await _drain_until(comm, "error")
            assert err == {
                "type": "error",
                "code": "firmware_update_pending",
                "message": lockmod.FIRMWARE_DRAIN_MESSAGE,
            }
            evt = await _drain_until(comm, "lock")
            assert evt["state"] == "free"
            assert evt["you_hold"] is False
            await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id is None


@pytest.mark.django_db(transaction=True)
def test_request_and_transfer_while_draining():
    """A request to another holder is the normal flow (no drain error, the holder is
    asked); only the transfer — the actual new grant — gets the drain refusal."""
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cdr2", status="online")
    holder, other = _member("cdr2h"), _member("cdr2o")
    lockmod.acquire(station, holder)

    async def scenario():
        with (
            mock.patch(DRAINING, return_value=True),
            mock.patch(DRAIN_STATUS, return_value=ACTIVE),
        ):
            hc = _browser_comm(holder, station.id)
            assert (await hc.connect())[0] is True
            await _drain_until(hc, "lock")
            oc = _browser_comm(other, station.id)
            assert (await oc.connect())[0] is True
            await _drain_until(oc, "lock")

            await oc.send_json_to({"type": "lock_request"})
            req = await _drain_until(hc, "control_requested")
            assert req["requester"]["id"] == other.id
            assert await oc.receive_nothing(timeout=0.3)

            await hc.send_json_to({"type": "lock_transfer", "to_user_id": other.id})
            err = await _drain_until(hc, "error")
            assert err["code"] == "firmware_update_pending"
            assert err["message"] == lockmod.FIRMWARE_DRAIN_MESSAGE
            await hc.disconnect()
            await oc.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id == holder.id


@pytest.mark.django_db(transaction=True)
def test_acquire_of_held_lock_while_draining_sends_no_drain_error():
    """Refused because someone else holds the lock — not because of the drain."""
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cdr2b", status="online")
    holder, other = _member("cdr2bh"), _member("cdr2bo")
    lockmod.acquire(station, holder)

    async def scenario():
        with (
            mock.patch(DRAINING, return_value=True),
            mock.patch(DRAIN_STATUS, return_value=ACTIVE),
        ):
            oc = _browser_comm(other, station.id)
            assert (await oc.connect())[0] is True
            await _drain_until(oc, "lock")
            await oc.send_json_to({"type": "lock_acquire"})
            evt = await oc.receive_json_from()
            assert evt["type"] == "lock"
            assert evt["state"] == "held"
            await oc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_preempt_of_free_lock_while_draining_refused():
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cdr2c", status="online")
    admin = User.objects.create(username="cdr2cadmin", membership_level=User.MembershipLevel.ADMIN)

    async def scenario():
        with (
            mock.patch(DRAINING, return_value=True),
            mock.patch(DRAIN_STATUS, return_value=ACTIVE),
        ):
            comm = _browser_comm(admin, station.id)
            assert (await comm.connect())[0] is True
            await _drain_until(comm, "lock")
            await comm.send_json_to({"type": "lock_preempt"})
            err = await _drain_until(comm, "error")
            assert err["code"] == "firmware_update_pending"
            evt = await _drain_until(comm, "lock")
            assert evt["state"] == "free"
            await comm.disconnect()

    asyncio.run(scenario())
    assert lockmod.get_or_create_lock(station).holder_id is None


@pytest.mark.django_db(transaction=True)
def test_refusal_without_drain_sends_no_drain_error():
    """A plain 'held by someone else' refusal keeps today's behaviour: no error."""
    from apps.control import lock as lockmod

    station = Station.objects.create(name="cdr3", status="online")
    holder, other = _member("cdr3h"), _member("cdr3o")
    lockmod.acquire(station, holder)

    async def scenario():
        oc = _browser_comm(other, station.id)
        assert (await oc.connect())[0] is True
        await _drain_until(oc, "lock")
        await oc.send_json_to({"type": "lock_acquire"})
        evt = await _drain_until(oc, "lock")
        assert evt["state"] == "held"
        assert evt["drain"] is None
        await oc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_lock_frame_carries_drain_since():
    station = Station.objects.create(name="cdr4", status="online")
    user = _member("cdr4u")

    async def scenario():
        with mock.patch(DRAIN_STATUS, return_value=ACTIVE):
            comm = _browser_comm(user, station.id)
            assert (await comm.connect())[0] is True
            evt = await _drain_until(comm, "lock")
            assert evt["drain"] == {"since": SINCE.isoformat()}
            await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_lock_frame_drain_null_when_idle():
    station = Station.objects.create(name="cdr5", status="online")
    user = _member("cdr5u")

    async def scenario():
        comm = _browser_comm(user, station.id)
        assert (await comm.connect())[0] is True
        evt = await _drain_until(comm, "lock")
        assert "drain" in evt
        assert evt["drain"] is None
        await comm.send_json_to({"type": "lock_acquire"})
        evt = await _drain_until(comm, "lock")
        assert evt["state"] == "held"
        assert evt["drain"] is None
        await comm.disconnect()

    asyncio.run(scenario())
