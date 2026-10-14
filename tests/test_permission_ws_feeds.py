"""Unified permissions on the status feeds + terminal (spec 2026-10-14 §3.4, §5.2).

No pytest-asyncio: scenarios run via asyncio.run() in transactional tests.
"""

import asyncio

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache

from apps.accounts.models import AccountAuditLog, User
from apps.audio.consumers import AgentAudioConsumer
from apps.control.consumers import AgentControlConsumer
from apps.stations.models import Station
from apps.tunnel.consumers import AgentTerminalConsumer
from config.asgi import application

APPLICANT = User.MembershipLevel.APPLICANT
STAFF = User.MembershipLevel.STAFF


def _user(name, level):
    return User.objects.create(username=name, membership_level=level)


def _comm(path, user=None):
    comm = WebsocketCommunicator(application, path)
    if user is not None:
        comm.scope["user"] = user
    return comm


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.mark.django_db(transaction=True)
def test_applicant_can_subscribe_deployment_feed():
    applicant = _user("feed-app", APPLICANT)

    async def scenario():
        comm = _comm("/ws/deployments/status/", applicant)
        connected, _ = await comm.connect()
        assert connected is True
        # Not closed: the group join happened and nothing was sent.
        assert await comm.receive_nothing(timeout=0.2)
        await get_channel_layer().group_send(
            "deployment_status",
            {"type": "deployment_update", "data": {"deployment_id": 1, "status": "x"}},
        )
        assert (await comm.receive_json_from())["deployment_id"] == 1
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_applicant_can_subscribe_station_feed():
    applicant = _user("feed-app2", APPLICANT)

    async def scenario():
        comm = _comm("/ws/stations/status/", applicant)
        connected, _ = await comm.connect()
        assert connected is True
        assert await comm.receive_nothing(timeout=0.2)
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_station_feed_hides_ip_from_non_managers():
    applicant = _user("ip-app", APPLICANT)
    staff = _user("ip-staff", STAFF)
    payload = {"id": 1, "status": "online", "last_ip_address": "10.1.2.3"}

    async def scenario():
        a = _comm("/ws/stations/status/", applicant)
        s = _comm("/ws/stations/status/", staff)
        assert (await a.connect())[0] and (await s.connect())[0]
        # accept precedes the group join (gate runs after accept): let it settle
        assert await a.receive_nothing(timeout=0.2) and await s.receive_nothing(timeout=0.2)
        await get_channel_layer().group_send(
            "station_status", {"type": "station_update", "data": payload}
        )
        frame_a = await a.receive_json_from()
        frame_s = await s.receive_json_from()
        assert "last_ip_address" not in frame_a
        assert frame_a["status"] == "online"
        assert frame_s["last_ip_address"] == "10.1.2.3"
        await a.disconnect()
        await s.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_anonymous_station_feed_4401_not_logged():
    async def scenario():
        from django.contrib.auth.models import AnonymousUser

        comm = _comm("/ws/stations/status/", AnonymousUser())
        connected, code = await comm.connect()
        assert connected is False
        assert code == 4401

    asyncio.run(scenario())
    assert not AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.PERMISSION_DENIED
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_staff_terminal_connect_denied_with_frame():
    station = Station.objects.create(name="t-deny", status="online")
    staff = _user("term-staff", STAFF)

    async def scenario():
        comm = _comm(f"/ws/terminal/{station.id}/", staff)
        assert (await comm.connect())[0] is True
        frame = await comm.receive_json_from()
        assert frame["type"] == "error"
        assert frame["code"] == "forbidden"
        assert frame["message"]
        out = await comm.receive_output()
        assert out == {"type": "websocket.close", "code": 4403}
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_ws_connect_denials_are_deduplicated():
    station = Station.objects.create(name="t-dedup", status="online")
    staff = _user("dedup-staff", STAFF)

    async def scenario():
        for _ in range(5):
            comm = _comm(f"/ws/terminal/{station.id}/", staff)
            await comm.connect()
            await comm.receive_json_from()
            await comm.disconnect()

    asyncio.run(scenario())
    rows = AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.PERMISSION_DENIED)
    assert rows.count() == 1
    row = rows.get()
    assert row.details["count"] == 5
    assert row.details["channel"] == "ws"


def test_agent_consumers_declare_exemption():
    for cls in (AgentControlConsumer, AgentAudioConsumer, AgentTerminalConsumer):
        assert cls.permission_exempt == "agent: ed25519"


def _demote(user):
    User.objects.filter(pk=user.pk).update(membership_level=STAFF)


@pytest.mark.django_db(transaction=True)
def test_demoted_admin_terminal_input_denied_and_session_closed():
    from asgiref.sync import sync_to_async

    from apps.tunnel.models import TerminalSession

    station = Station.objects.create(name="t-demote", status="online")
    admin = _user("term-admin", User.MembershipLevel.ADMIN)

    async def scenario():
        layer = get_channel_layer()
        spy = "agent-spy-demote"
        await layer.group_add(f"terminal_{station.id}_agent", spy)
        comm = _comm(f"/ws/terminal/{station.id}/", admin)
        assert (await comm.connect())[0] is True
        assert (await layer.receive(spy))["type"] == "terminal_ensure"
        await sync_to_async(_demote)(admin)
        await comm.send_json_to({"type": "input", "data": "rm -rf /\n"})
        frame = await comm.receive_json_from()
        assert frame["code"] == "forbidden"
        out = await comm.receive_output()
        assert out == {"type": "websocket.close", "code": 4403}
        await comm.disconnect()
        # Shell torn down (terminal_close), input never forwarded.
        assert (await layer.receive(spy))["type"] == "terminal_close"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(spy), timeout=0.3)

    asyncio.run(scenario())
    assert TerminalSession.objects.get(station=station).status == "closed"


@pytest.mark.django_db(transaction=True)
def test_demoted_admin_idle_terminal_closed_by_keepalive(monkeypatch):
    from asgiref.sync import sync_to_async

    from apps.tunnel import consumers as tunnel_consumers

    monkeypatch.setattr(tunnel_consumers, "TERMINAL_SESSION_KEEPALIVE_SECONDS", 0.2)
    station = Station.objects.create(name="t-idle", status="online")
    admin = _user("term-admin2", User.MembershipLevel.ADMIN)

    async def scenario():
        comm = _comm(f"/ws/terminal/{station.id}/", admin)
        assert (await comm.connect())[0] is True
        await sync_to_async(_demote)(admin)
        frame = await comm.receive_json_from()
        assert frame["code"] == "forbidden"
        out = await comm.receive_output()
        assert out == {"type": "websocket.close", "code": 4403}
        await comm.disconnect()

    asyncio.run(scenario())
