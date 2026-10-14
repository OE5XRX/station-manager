"""Passive WS feeds re-check their connect permission periodically (final review, item 6).

A viewer who only listens never sends a frame, so per-frame checks never fire:
``AreaConsumerMixin.start_periodic_recheck`` re-evaluates ``connect_verb`` with a
fresh user every ``PERIODIC_RECHECK_SECONDS`` (monkeypatched short here), closes
with 4403 + forbidden frame when it is lost (audited once) and lets
StationStatusConsumer refresh ``can_see_ip``. No pytest-asyncio: asyncio.run().
"""

import asyncio

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache

from apps.accounts import permissions
from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Station
from config.asgi import application

INTERVAL = 0.05
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _fast_recheck(monkeypatch):
    cache.clear()
    monkeypatch.setattr(permissions, "PERIODIC_RECHECK_SECONDS", INTERVAL)


def _comm(path, user):
    comm = WebsocketCommunicator(application, path)
    comm.scope["user"] = user
    return comm


async def _deactivate(user):
    await sync_to_async(User.objects.filter(pk=user.pk).update)(is_active=False)


async def _expect_forbidden_close(comm):
    frame = await comm.receive_json_from(timeout=2)
    while frame.get("code") != "forbidden":  # skip feed frames (inventory/lock/...)
        frame = await comm.receive_json_from(timeout=2)
    assert frame["type"] == "error"
    out = await comm.receive_output(timeout=2)
    assert out == {"type": "websocket.close", "code": 4403}


def _denials():
    return AccountAuditLog.objects.filter(event_type="permission_denied")


def test_deactivated_deployment_viewer_closed_after_interval_audited_once():
    user = User.objects.create(username="rc-dep", membership_level="applicant")

    async def scenario():
        comm = _comm("/ws/deployments/status/", user)
        assert (await comm.connect())[0] is True
        assert await comm.receive_nothing(timeout=INTERVAL * 3)  # still allowed: silent
        await _deactivate(user)
        await _expect_forbidden_close(comm)
        await asyncio.sleep(INTERVAL * 4)  # no further checks after the close
        await comm.disconnect()

    asyncio.run(scenario())
    assert _denials().count() == 1
    assert _denials().get().details["count"] == 1


def test_deactivated_station_status_viewer_closed():
    user = User.objects.create(username="rc-st", membership_level="applicant")

    async def scenario():
        comm = _comm("/ws/stations/status/", user)
        assert (await comm.connect())[0] is True
        await _deactivate(user)
        await _expect_forbidden_close(comm)
        await comm.disconnect()

    asyncio.run(scenario())


def test_demoted_staff_stops_receiving_ips_after_interval():
    staff = User.objects.create(username="rc-staff", membership_level="staff")
    payload = {"id": 1, "status": "online", "last_ip_address": "10.1.2.3"}

    async def scenario():
        comm = _comm("/ws/stations/status/", staff)
        assert (await comm.connect())[0] is True
        assert await comm.receive_nothing(timeout=0.1)  # connect() returns at accept()
        layer = get_channel_layer()
        await layer.group_send("station_status", {"type": "station_update", "data": payload})
        assert (await comm.receive_json_from())["last_ip_address"] == "10.1.2.3"
        await sync_to_async(User.objects.filter(pk=staff.pk).update)(membership_level="member")
        await asyncio.sleep(INTERVAL * 4)
        await layer.group_send("station_status", {"type": "station_update", "data": payload})
        assert "last_ip_address" not in await comm.receive_json_from()
        await comm.disconnect()

    asyncio.run(scenario())
    assert not _denials().exists()  # losing the IP privilege is not a denial


def test_deactivated_control_viewer_closed():
    station = Station.objects.create(name="rc-ctl", status="online")
    user = User.objects.create(username="rc-ctl", membership_level="member")

    async def scenario():
        comm = _comm(f"/ws/control/{station.id}/", user)
        assert (await comm.connect())[0] is True
        await _deactivate(user)
        await _expect_forbidden_close(comm)
        await comm.disconnect()

    asyncio.run(scenario())


def test_deactivated_audio_listener_closed():
    station = Station.objects.create(name="rc-aud", status="online")
    user = User.objects.create(username="rc-aud", membership_level="applicant")

    async def scenario():
        comm = _comm(f"/ws/audio/{station.id}/", user)
        assert (await comm.connect())[0] is True
        await _deactivate(user)
        await _expect_forbidden_close(comm)
        await comm.disconnect()

    asyncio.run(scenario())


def test_recheck_task_cancelled_on_disconnect(monkeypatch):
    from apps.deployments.consumers import DeploymentStatusConsumer

    user = User.objects.create(username="rc-cancel", membership_level="applicant")
    instances = []
    original = DeploymentStatusConsumer.connect

    async def spy_connect(self):
        instances.append(self)
        await original(self)

    async def scenario():
        comm = WebsocketCommunicator(DeploymentStatusConsumer.as_asgi(), "/ws/deployments/status/")
        comm.scope["user"] = user
        assert (await comm.connect())[0] is True
        assert await comm.receive_nothing(timeout=0.1)  # connect() returns at accept()
        task = instances[0]._recheck_task
        assert task is not None and not task.done()
        await comm.disconnect()
        assert task.cancelled() or task.done()
        assert instances[0]._recheck_task is None

    monkeypatch.setattr(DeploymentStatusConsumer, "connect", spy_connect)
    asyncio.run(scenario())


def _flaky_fresh_user(monkeypatch, failures):
    """Patch AreaConsumerMixin.fresh_user to raise ``failures[0]`` more times once armed."""
    original = permissions.AreaConsumerMixin.fresh_user

    async def fresh_user(self):
        if failures["armed"] and failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("db connection lost")
        return await original(self)

    monkeypatch.setattr(permissions.AreaConsumerMixin, "fresh_user", fresh_user)


def test_recheck_failing_repeatedly_closes_feed(monkeypatch):
    """Re-review C1: a re-check that keeps failing must not leave the feed open
    and unchecked — after RECHECK_MAX_FAILURES consecutive errors it closes."""
    failures = {"armed": False, "left": 10**6}
    _flaky_fresh_user(monkeypatch, failures)
    user = User.objects.create(username="rc-fail", membership_level="applicant")

    async def scenario():
        comm = _comm("/ws/deployments/status/", user)
        assert (await comm.connect())[0] is True
        assert await comm.receive_nothing(timeout=0.1)
        failures["armed"] = True
        frame = await comm.receive_json_from(timeout=2)
        assert frame["type"] == "error" and frame["code"] == "recheck_failed"
        assert await comm.receive_output(timeout=2) == {"type": "websocket.close", "code": 1011}
        await comm.disconnect()

    asyncio.run(scenario())


def test_single_recheck_failure_is_retried(monkeypatch):
    failures = {"armed": False, "left": 1}
    _flaky_fresh_user(monkeypatch, failures)
    user = User.objects.create(username="rc-flaky", membership_level="applicant")

    async def scenario():
        comm = _comm("/ws/deployments/status/", user)
        assert (await comm.connect())[0] is True
        assert await comm.receive_nothing(timeout=0.1)
        failures["armed"] = True
        assert await comm.receive_nothing(timeout=INTERVAL * 6)  # recovered, still open
        await _deactivate(user)  # and still checked
        await _expect_forbidden_close(comm)
        await comm.disconnect()

    asyncio.run(scenario())
