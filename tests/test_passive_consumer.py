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
from tests.test_passive_profiles import make_descriptor

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
