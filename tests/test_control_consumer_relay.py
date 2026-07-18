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


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _until(comm, mtype, tries=8):
    """Drain frames from comm until one with the given type arrives."""
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw {mtype}")


async def _until_lock_held(comm, tries=8):
    """Drain frames until a lock frame with state=='held' arrives.

    Needed because on connect the browser receives an initial free-state lock
    frame; after lock_acquire a second lock frame arrives with state=='held'.
    """
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == "lock" and msg.get("state") == "held":
            return msg
    raise AssertionError("never saw lock(state=held)")


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


# ---------------------------------------------------------------------------
# E2E relay tests (Task 7)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_full_relay_command_result_state_to_all_viewers(control_agent_auth):
    """Full loop: mock agent connects, holder acquires lock, sends command,
    agent receives it verbatim, replies with result + state, both holder
    and a viewer browser see the state broadcast, and last_state is persisted.
    """
    from apps.control.models import StationModule

    station = Station.objects.create(name="e2e1", status="online")
    holder = User.objects.create(username="he", membership_level=User.MembershipLevel.MEMBER)
    viewer = User.objects.create(username="ve", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        # Connect mock agent and seed registry with an fm0 module.
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to({
            "v": V, "type": "inventory",
            "slots": [{"slot": "slot0", "modules": [{
                "module": "fm0", "identity": {"type": "fm"},
                "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
                "state": {"frequency": 145.0},
            }]}],
        })

        # Connect holder and viewer browsers.
        hc = _browser(holder, station.id)
        vc = _browser(viewer, station.id)
        assert (await hc.connect())[0] is True
        assert (await vc.connect())[0] is True

        # Holder acquires the lock.  On connect the holder browser already
        # received an initial free-state {type:"lock"} frame.  _until_lock_held
        # skips that frame and any other non-held lock frames to land on the
        # post-acquire held-state frame.
        await hc.send_json_to({"type": "lock_acquire"})
        lock_evt = await _until_lock_held(hc)
        assert lock_evt["state"] == "held"
        assert lock_evt["you_hold"] is True

        # Holder sends a command to set frequency.
        await hc.send_json_to({
            "type": "command", "request_id": "rq1",
            "slot": "slot0", "module": "fm0",
            "capability": "frequency", "op": "set", "value": 146.5,
        })

        # The MOCK AGENT receives the relayed command frame verbatim.
        got = await agent.receive_json_from()
        assert got["type"] == "command"
        assert got["request_id"] == "rq1"
        assert got["value"] == 146.5

        # Agent replies with result (cancels the timeout timer) then state.
        await agent.send_json_to({
            "v": V, "type": "result",
            "request_id": "rq1", "ok": True, "value": 146.5,
        })
        await agent.send_json_to({
            "v": V, "type": "state",
            "slot": "slot0", "module": "fm0",
            "values": {"frequency": 146.5}, "ts": 1.0,
        })

        # Both holder and viewer see the state broadcast.
        hstate = await _until(hc, "state")
        vstate = await _until(vc, "state")
        assert hstate["values"]["frequency"] == 146.5
        assert vstate["values"]["frequency"] == 146.5

        await hc.disconnect()
        await vc.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())

    # Settings were persisted to the database.
    m = StationModule.objects.get(station=station, slot="slot0", module_id="fm0")
    assert m.last_state == {"frequency": 146.5}


@pytest.mark.django_db(transaction=True)
def test_command_timeout_pushes_error(control_agent_auth, settings):
    """When CONTROL_COMMAND_TIMEOUT_SECONDS=0, the consumer's timeout task
    fires immediately and pushes {type:'error', error:{code:'timeout'}} to
    the holder.  No importlib.reload needed because _command_timeout reads
    settings at call-time.
    """
    settings.CONTROL_COMMAND_TIMEOUT_SECONDS = 0  # fire immediately

    station = Station.objects.create(name="e2e2", status="online")
    holder = User.objects.create(username="ht", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        hc = _browser(holder, station.id)
        assert (await hc.connect())[0] is True

        # Acquire the lock.  Wait for the held-state lock frame (skipping the
        # initial free-state frame already in the buffer on connect).
        await hc.send_json_to({"type": "lock_acquire"})
        await _until_lock_held(hc)

        # Send a command — no agent connected so no result will ever arrive.
        await hc.send_json_to({
            "type": "command", "request_id": "to1",
            "slot": "slot0", "module": "fm0",
            "capability": "frequency", "op": "set", "value": 1.0,
        })

        # The timeout fires (sleep(0) yields to the event loop once) and the
        # holder receives a timeout error for exactly that request_id.
        err = await _until(hc, "error")
        assert err["error"]["code"] == "timeout"
        assert err["request_id"] == "to1"

        await hc.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_subscribe_relayed_without_lock(control_agent_auth):
    """A viewer who does NOT hold the lock can send subscribe/unsubscribe.
    Those frames are access-gated (not lock-gated), so they must be relayed
    to the agent group even without a lock.
    """
    station = Station.objects.create(name="e2e3", status="online")
    viewer = User.objects.create(username="vsub", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-e2e3"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        vc = _browser(viewer, station.id)
        assert (await vc.connect())[0] is True

        # Viewer does NOT acquire the lock.
        subscribe_frame = {
            "type": "subscribe",
            "slot": "slot0",
            "module": "fm0",
            "capabilities": ["rssi"],
            "interval_ms": 1000,
        }
        await vc.send_json_to(subscribe_frame)

        # The frame MUST arrive in the agent group (access-gated, not lock-gated).
        relayed = await asyncio.wait_for(layer.receive(agent_spy), timeout=2.0)
        assert relayed["type"] == "control.to_agent"
        assert relayed["frame"]["type"] == "subscribe"
        assert relayed["frame"]["slot"] == "slot0"
        assert relayed["frame"]["capabilities"] == ["rssi"]

        await vc.disconnect()

    asyncio.run(scenario())
