"""Channels tests for TerminalConsumer.connect() lifecycle.

This repo has NO pytest-asyncio. Async Channels scenarios are driven with
plain sync test functions marked ``@pytest.mark.django_db(transaction=True)``:
DB objects are created with normal ORM in the sync body, then the
``WebsocketCommunicator`` scenario runs inside a nested ``async def scenario()``
executed via ``asyncio.run(scenario())``. Do NOT use ``@pytest.mark.asyncio``.
"""

import asyncio
from datetime import timedelta

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import User
from apps.stations.models import Station
from apps.tunnel.models import TerminalSession
from config.asgi import application


def _communicator(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/terminal/{station_id}/")
    comm.scope["user"] = user
    return comm


@pytest.mark.django_db(transaction=True)
def test_non_admin_gets_error_message_then_close():
    """A staff (internal but not admin) user is accepted then errored 4403."""
    station = Station.objects.create(name="s-nonadmin", status="online")
    user = User.objects.create(username="u_staff", membership_level=User.MembershipLevel.STAFF)

    async def scenario():
        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True  # accept happened before the reject
        msg = await comm.receive_json_from()
        assert msg["type"] == "error"
        assert msg["code"] == 4403
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_offline_station_gets_error():
    """An admin connecting to an offline station is accepted then errored 4409."""
    station = Station.objects.create(name="s-offline", status="offline")
    user = User.objects.create(username="u_admin_off", membership_level=User.MembershipLevel.ADMIN)

    async def scenario():
        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        msg = await comm.receive_json_from()
        assert msg["type"] == "error"
        assert msg["code"] == 4409
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_stale_sessions_do_not_block():
    """Ancient 'active' rows with old last_seen are reaped, not counted."""
    station = Station.objects.create(name="s-stale", status="online")
    user = User.objects.create(
        username="u_admin_stale", membership_level=User.MembershipLevel.ADMIN
    )
    old = timezone.now() - timedelta(hours=5)
    TerminalSession.objects.create(station=station, user=user, status="active", last_seen=old)
    TerminalSession.objects.create(station=station, user=user, status="active", last_seen=old)

    async def scenario():
        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True  # not rejected with 4429
        await comm.disconnect()

    asyncio.run(scenario())

    reaped = TerminalSession.objects.filter(station=station, status="closed").count()
    assert reaped >= 2


@pytest.mark.django_db(transaction=True)
def test_disconnect_does_not_close_shell():
    """A transient browser disconnect must NOT tell the agent to close the shell."""
    station = Station.objects.create(name="s-noclose", status="online")
    user = User.objects.create(
        username="u_admin_noclose", membership_level=User.MembershipLevel.ADMIN
    )

    async def scenario():
        layer = get_channel_layer()
        spy = "agent-spy-noclose"
        await layer.group_add(f"terminal_{station.id}_agent", spy)

        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True

        # Drain the terminal_ensure sent to the agent group on connect.
        ensure = await layer.receive(spy)
        assert ensure["type"] == "terminal_ensure"

        await comm.disconnect()

        # No further message (i.e. no terminal_close) must reach the agent.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(spy), timeout=0.3)

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_browser_restart_forwards_terminal_restart():
    """A browser {type:"restart"} is forwarded as terminal_restart to the agent."""
    station = Station.objects.create(name="s-restart", status="online")
    user = User.objects.create(
        username="u_admin_restart", membership_level=User.MembershipLevel.ADMIN
    )

    async def scenario():
        layer = get_channel_layer()
        spy = "agent-spy-restart"
        await layer.group_add(f"terminal_{station.id}_agent", spy)

        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True

        # Drain the terminal_ensure sent on connect.
        await layer.receive(spy)

        await comm.send_json_to({"type": "restart"})

        msg = await layer.receive(spy)
        assert msg["type"] == "terminal_restart"

        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_anonymous_gets_error_message_then_close():
    """An unauthenticated connect is accepted then errored 4401, so the browser
    shows a reason instead of an opaque 1006 and does not reconnect-loop."""
    from django.contrib.auth.models import AnonymousUser

    station = Station.objects.create(name="s-anon", status="online")

    async def scenario():
        comm = _communicator(AnonymousUser(), station.id)
        connected, _ = await comm.connect()
        assert connected is True
        msg = await comm.receive_json_from()
        assert msg["type"] == "error"
        assert msg["code"] == 4401
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_agent_shell_closed_notifies_browser_without_closing():
    """An agent 'closed' frame (shell exited) reaches the browser as
    {type:"closed"} but must NOT close the browser WS (persist model)."""
    station = Station.objects.create(name="s-shellclosed", status="online")
    user = User.objects.create(username="u_admin_sc", membership_level=User.MembershipLevel.ADMIN)

    async def scenario():
        layer = get_channel_layer()
        spy = "agent-spy-sc"
        await layer.group_add(f"terminal_{station.id}_agent", spy)

        comm = _communicator(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        # Draining the connect-time ensure guarantees connect() ran past
        # group_add (which precedes the ensure group_send), so the browser is
        # in the group before we inject the shell-closed frame below.
        assert (await layer.receive(spy))["type"] == "terminal_ensure"

        # Simulate the server relaying an agent shell-exit to the browser group.
        await layer.group_send(
            f"terminal_{station.id}",
            {"type": "terminal_shell_closed", "reason": "shell exited with code 0"},
        )
        msg = await comm.receive_json_from()
        assert msg["type"] == "closed"
        assert "shell exited" in msg["reason"]
        # WS must stay open: no further frame (esp. no close) should arrive.
        assert await comm.receive_nothing(timeout=0.2) is True
        await comm.disconnect()

    asyncio.run(scenario())
