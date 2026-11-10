"""Passive-services supervisor wired into the control consumers (real websockets)."""

import asyncio
import json

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.control import constants
from apps.control.models import PersistedCapability
from apps.passive.models import (
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)
from apps.passive.supervisor import PassiveSupervisor
from apps.stations.models import Station
from config.asgi import application
from tests.test_passive_profiles import make_descriptor, make_module

pytestmark = pytest.mark.django_db(transaction=True)

State = PassiveServiceRun.State


def _agent(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


def _inventory(features=("decoders",), volume=None):
    state = {} if volume is None else {"volume": volume}
    msg = {
        "v": 1,
        "type": "inventory",
        "slots": [
            {
                "slot": 1,
                "modules": [
                    {
                        "module": "fm-1",
                        "identity": {"type": "fm_transceiver"},
                        "capabilities": [
                            {**c, "type": "int"} if c["name"] == "volume" else c
                            for c in make_descriptor()
                        ],
                        "state": state,
                        "online": True,
                    }
                ],
            }
        ],
    }
    if features:
        msg["features"] = list(features)
    return msg


@pytest.fixture
def setup(control_agent_auth, monkeypatch):
    monkeypatch.setattr(constants, "LOCK_SWEEP_INTERVAL_SECONDS", 0.05)
    station = Station.objects.create(name="pa-cons", status="online")
    PassiveServiceConfig.objects.create(service="aprs", enabled=True, idle_minutes=1)
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    user = User.objects.create(username="pa-user", membership_level=User.MembershipLevel.MEMBER)
    return station, user


async def serve(agent, until, seen=None, tries=80):
    """Play the agent: ack every ``passive-`` request, collect frames until ``until``."""
    seen = [] if seen is None else seen
    for _ in range(tries):
        frame = await agent.receive_json_from(timeout=3)
        seen.append(frame)
        rid = frame.get("request_id")
        if isinstance(rid, str) and rid.startswith("passive-"):
            await agent.send_json_to({"type": "result", "request_id": rid, "ok": True})
        if until(frame):
            return seen
    raise AssertionError(f"expected frame never arrived: {seen}")


@database_sync_to_async
def _run(station):
    return PassiveServiceRun.objects.filter(station=station).order_by("-pk").first()


async def _wait_run_state(station, state):
    for _ in range(100):
        run = await _run(station)
        if run is not None and run.state == state:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"run never reached {state}")


def test_start_then_user_acquire_hands_over(setup):
    station, user = setup

    async def scenario():
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory())
        await agent.send_json_to({"v": 1, "type": "decoder_snapshot", "decoders": []})
        seen = await serve(agent, lambda f: f.get("type") == "decoder_start")
        assert [f["capability"] for f in seen if f["type"] == "command"][0] == "frequency"
        await agent.send_json_to(
            {"v": 1, "type": "decoder_state", "decoder": "aprs", "slot": 1, "state": "running"}
        )
        await _wait_run_state(station, State.RUNNING)

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        frames = []
        while True:
            frames.append(await browser.receive_json_from(timeout=2))
            if frames[-1]["type"] == "passive_state":
                break
        assert frames[-1]["state"] == "running" and frames[-1]["slot"] == "1"
        # No system command result ever reaches a browser.
        assert not [f for f in frames if f["type"] == "result"]

        await browser.send_json_to({"type": "lock_acquire"})
        seen = await serve(
            agent, lambda f: f.get("capability") == "squelch" and f.get("value") == 1
        )
        types = [f["type"] for f in seen]
        assert types[0] == "decoder_stop"
        assert (await _wait_run_state(station, State.STOPPED)).end_reason == "acquired"
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
    assert PersistedCapability.objects.count() == 0


def test_passive_result_is_not_broadcast_but_normal_result_is(setup):
    station, user = setup

    async def scenario():
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        while (await browser.receive_json_from(timeout=2))["type"] != "lock":
            pass
        await agent.send_json_to({"type": "result", "request_id": "passive-x", "ok": True})
        await agent.send_json_to({"type": "result", "request_id": "u-1", "ok": True})
        results = []
        while not results or results[-1]["request_id"] != "u-1":
            msg = await browser.receive_json_from(timeout=2)
            if msg["type"] == "result":
                results.append(msg)
        assert [r["request_id"] for r in results] == ["u-1"]
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


def test_reapply_skips_passive_profile_caps(setup, monkeypatch):
    station, user = setup
    PassiveServiceConfig.objects.update(enabled=False)  # keep the supervisor itself quiet
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm-1", capability="volume", value=7
    )

    async def inventory_frames(protected):
        monkeypatch.setattr(
            PassiveSupervisor, "protected_caps", lambda self: protected, raising=True
        )
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory(volume=3))
        try:
            if await agent.receive_nothing(timeout=0.5):
                return []
            return [json.loads((await agent.receive_output())["text"])]
        finally:
            await agent.disconnect()

    free = asyncio.run(inventory_frames(set()))
    assert [(f["capability"], f["value"]) for f in free] == [("volume", 7)]
    held = asyncio.run(inventory_frames({("1", "fm-1", "volume")}))
    assert held == []


def test_browser_never_sees_a_run_state_without_a_run(setup):
    station, user = setup

    async def scenario():
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        states = []
        for _ in range(3):  # inventory, lock, then whatever the supervisor reports
            if await browser.receive_nothing(timeout=0.3):
                break
            msg = json.loads((await browser.receive_output())["text"])
            if msg["type"] == "passive_state":
                states.append(msg["state"])
        assert set(states) <= {"off"}
        await browser.disconnect()

    asyncio.run(scenario())


def test_browser_sees_agent_offline_run_state(setup):
    station, user = setup
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.AGENT_OFFLINE
    )

    async def scenario():
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        while True:
            msg = await browser.receive_json_from(timeout=2)
            if msg["type"] == "passive_state":
                break
        assert (msg["state"], msg["slot"]) == ("agent_offline", "1")
        await browser.disconnect()

    asyncio.run(scenario())


def test_agent_disconnect_keeps_running_run_as_agent_offline(setup):
    station, user = setup

    async def scenario():
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory())
        await agent.send_json_to({"v": 1, "type": "decoder_snapshot", "decoders": []})
        await serve(agent, lambda f: f.get("type") == "decoder_start")
        await agent.send_json_to(
            {"v": 1, "type": "decoder_state", "decoder": "aprs", "slot": 1, "state": "running"}
        )
        await _wait_run_state(station, State.RUNNING)
        await agent.disconnect()
        await _wait_run_state(station, State.AGENT_OFFLINE)

    asyncio.run(scenario())


def test_reconnect_does_not_reapply_over_a_surviving_runs_profile(setup):
    """The inventory of a reconnect arrives before the snapshot: the persisted volume must
    not overwrite the profile volume of an agent_offline run whose decoder survived."""
    station, user = setup
    PassiveServiceConfig.objects.update(
        profile={"frequency": 144.8, "squelch": 0, "volume": 3, "rx_tone": "none"}
    )
    PersistedCapability.objects.create(
        station=station, slot="1", module_id="fm-1", capability="volume", value=7
    )
    make_module(station)  # module rows persist across connections
    PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="1", module_id="fm-1", state=State.AGENT_OFFLINE
    )

    async def scenario():
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory(volume=3))
        stray = []
        while not await agent.receive_nothing(timeout=0.5):
            stray.append(json.loads((await agent.receive_output())["text"]))
        await agent.disconnect()
        return stray

    frames = asyncio.run(scenario())
    assert not [f for f in frames if f.get("capability") == "volume"]


def test_browser_command_with_reserved_request_id_is_rejected(setup):
    station, user = setup

    async def scenario():
        agent = _agent(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        while (await browser.receive_json_from(timeout=2))["type"] != "lock":
            pass
        await browser.send_json_to({"type": "lock_acquire"})
        while (await browser.receive_json_from(timeout=2)).get("state") != "held":
            pass
        await browser.send_json_to(
            {
                "type": "command",
                "request_id": "passive-evil",
                "slot": 1,
                "module": "fm-1",
                "capability": "volume",
                "op": "set",
                "value": 3,
            }
        )
        while True:
            msg = await browser.receive_json_from(timeout=2)
            if msg.get("type") == "error":
                break
        assert msg["error"]["code"] == "invalid_request_id"
        assert await agent.receive_nothing(timeout=0.3)  # nothing relayed to the agent
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


def test_preempt_hook_is_silent_without_an_open_run(setup):
    from channels.layers import get_channel_layer

    from apps.passive.supervisor import preempt_hook

    station, user = setup

    async def scenario():
        layer = get_channel_layer()
        await layer.group_add(f"control_{station.id}_agent", "spy-pa")
        await preempt_hook(station, user, "acquired")
        assert await _group_empty(layer, "spy-pa")
        await database_sync_to_async(PassiveServiceRun.objects.create)(
            station=station, service="aprs", slot="1", module_id="fm-1", state=State.RUNNING
        )
        await preempt_hook(station, user, "acquired")
        msg = await layer.receive("spy-pa")
        assert msg == {"type": "control.passive_preempt", "reason": "acquired"}

    asyncio.run(scenario())


async def _group_empty(layer, channel):
    try:
        await asyncio.wait_for(layer.receive(channel), 0.2)
    except TimeoutError:
        return True
    return False
