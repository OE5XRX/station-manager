"""Channels protocol-level contract tests for the D5 browser control panel.

These pin the server contract (apps/control/consumers.ControlConsumer) that the
Alpine controlPanel component in static/js/control-panel.js depends on. No
pytest-asyncio: async scenarios run via asyncio.run() inside a transactional
django_db test, mirroring tests/test_control_consumer_relay.py exactly (same
_agent_comm / _browser / _until / _until_lock_held helpers, same
control_agent_auth fixture).

Most of this behavior already exists in D4; several assertions pass immediately.
The point is to lock the shapes the JS wires against (command relay, result +
state, subscribe, not_locked, lock hand-off + control_requested + preempt,
agent-offline + lock-free). If any assertion here reveals a genuine server gap,
it is reported (D4 consumers are NOT modified from this task).
"""

import asyncio
import json

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application

V = 1

# An FM-style module descriptor, matching the shape D5 renders from: a numeric
# setting (frequency) plus telemetry (rssi) and the platform ptt action.
FM_MODULE = {
    "module": "fm0",
    "identity": {"type": "fm", "model": "SA818", "version": "1"},
    "capabilities": [
        {
            "name": "frequency",
            "kind": "setting",
            "type": "float",
            "ranges": [{"min": 144.0, "max": 146.0}],
            "step": 0.005,
            "unit": "MHz",
        },
        {
            "name": "rssi",
            "kind": "telemetry",
            "type": "int",
            "ranges": [{"min": -120, "max": 0}],
            "min_interval_ms": 500,
            "unit": "dBm",
        },
        {"name": "ptt", "kind": "action", "type": "bool"},
    ],
    "state": {"frequency": 145.0},
}


def _inventory_frame():
    return {
        "v": V,
        "type": "inventory",
        "slots": [{"slot": "slot0", "modules": [FM_MODULE]}],
    }


def _agent_comm(station_id):
    # Ed25519 verification is bypassed in tests via control_agent_auth.
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _until(comm, mtype, tries=8):
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw {mtype}")


async def _until_lock_held(comm, tries=8):
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == "lock" and msg.get("state") == "held":
            return msg
    raise AssertionError("never saw lock(state=held)")


async def _until_lock_where(comm, pred, tries=12):
    """Drain lock frames until one satisfies pred(frame)."""
    for _ in range(tries):
        msg = await comm.receive_json_from()
        if msg.get("type") == "lock" and pred(msg):
            return msg
    raise AssertionError("never saw matching lock frame")


# ---------------------------------------------------------------------------
# 1 + 2. Command relay + result/state confirm the pending-clearing loop the JS
#         uses (send -> pending; result clears pending; state confirms value).
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_command_relayed_and_result_state_confirm(control_agent_auth):
    station = Station.objects.create(name="jsws1", status="online")
    holder = User.objects.create(username="jh1", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory_frame())

        hc = _browser(holder, station.id)
        assert (await hc.connect())[0] is True

        await hc.send_json_to({"type": "lock_acquire"})
        held = await _until_lock_held(hc)
        assert held["you_hold"] is True

        # JS setValue for a float setting sends op:set with a JSON number.
        # Every browser->server frame carries the §7 envelope version (v); the
        # agent's parse_message DROPS any frame whose v != 1 (see the dedicated
        # test_agent_parse_message_requires_envelope_version below).
        await hc.send_json_to(
            {
                "v": V,
                "type": "command",
                "request_id": "js-rq1",
                "slot": "slot0",
                "module": "fm0",
                "capability": "frequency",
                "op": "set",
                "value": 145.5,
            }
        )

        got = await agent.receive_json_from()
        assert got["type"] == "command"
        assert got["v"] == V  # envelope version relayed through to the agent
        assert got["slot"] == "slot0"
        assert got["module"] == "fm0"
        assert got["capability"] == "frequency"
        assert got["op"] == "set"
        assert got["value"] == 145.5

        # Agent confirms: result (clears pending in JS) then state (authoritative value).
        await agent.send_json_to(
            {"v": V, "type": "result", "request_id": "js-rq1", "ok": True, "value": 145.5}
        )
        await agent.send_json_to(
            {
                "v": V,
                "type": "state",
                "slot": "slot0",
                "module": "fm0",
                "values": {"frequency": 145.5},
                "ts": 1.0,
            }
        )

        result = await _until(hc, "result")
        assert result["request_id"] == "js-rq1"
        assert result["ok"] is True
        state = await _until(hc, "state")
        assert state["values"]["frequency"] == 145.5

        await hc.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. Telemetry subscribe reaches the agent verbatim with caps + interval.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_subscribe_carries_capabilities_and_interval(control_agent_auth):
    station = Station.objects.create(name="jsws2", status="online")
    viewer = User.objects.create(username="jv2", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-jsws2"
        await layer.group_add(f"control_{station.id}_agent", agent_spy)

        vc = _browser(viewer, station.id)
        assert (await vc.connect())[0] is True

        # subscribe is access-gated, not lock-gated: no lock acquired here.
        await vc.send_json_to(
            {
                "v": V,
                "type": "subscribe",
                "slot": "slot0",
                "module": "fm0",
                "capabilities": ["rssi"],
                "interval_ms": 500,
            }
        )
        relayed = await asyncio.wait_for(layer.receive(agent_spy), timeout=2.0)
        assert relayed["type"] == "control.to_agent"
        frame = relayed["frame"]
        assert frame["type"] == "subscribe"
        assert frame["capabilities"] == ["rssi"]
        assert frame["interval_ms"] == 500

        await vc.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Non-holder command -> not_locked error (JS shows "You don’t have control").
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_non_holder_command_gets_not_locked(control_agent_auth):
    station = Station.objects.create(name="jsws3", status="online")
    viewer = User.objects.create(username="jv3", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        vc = _browser(viewer, station.id)
        assert (await vc.connect())[0] is True

        await vc.send_json_to(
            {
                "type": "command",
                "request_id": "nl1",
                "slot": "slot0",
                "module": "fm0",
                "capability": "frequency",
                "op": "set",
                "value": 145.0,
            }
        )
        err = await _until(vc, "error")
        assert err["request_id"] == "nl1"
        assert err["error"]["code"] == "not_locked"

        await vc.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Lock hand-off: A holds, B sees you_hold:false, B requests -> A prompted,
#    admin preempt -> A sees you_hold:false. Pins the lock UX contract.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_lock_handoff_request_and_preempt(control_agent_auth):
    station = Station.objects.create(name="jsws4", status="online")
    holder = User.objects.create(username="ja4", membership_level=User.MembershipLevel.MEMBER)
    # Admin so lock_preempt is authorized.
    admin = User.objects.create(username="jadmin4", membership_level=User.MembershipLevel.ADMIN)

    async def scenario():
        ac = _browser(holder, station.id)
        assert (await ac.connect())[0] is True
        await ac.send_json_to({"type": "lock_acquire"})
        await _until_lock_held(ac)

        # B connects and sees the held lock is not theirs.
        bc = _browser(admin, station.id)
        assert (await bc.connect())[0] is True
        b_lock = await _until_lock_where(
            bc, lambda m: m.get("state") == "held" and m.get("you_hold") is False
        )
        assert b_lock["holder_username"] == "ja4"

        # B requests control -> the holder (A) receives a control_requested prompt.
        await bc.send_json_to({"type": "lock_request"})
        req = await _until(ac, "control_requested")
        assert req["requester"]["username"] == "jadmin4"
        assert req["requester"]["id"] == admin.id

        # Admin preempts -> A now sees you_hold:false (control was taken).
        await bc.send_json_to({"type": "lock_preempt"})
        a_lost = await _until_lock_where(ac, lambda m: m.get("you_hold") is False)
        assert a_lost["you_hold"] is False

        await ac.disconnect()
        await bc.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. Agent disconnect while browser holds -> agent_offline + a free lock frame.
#    Drives the JS fail-safe (force PTT unkey, controls disable).
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_agent_offline_and_lock_free_on_agent_disconnect(control_agent_auth):
    station = Station.objects.create(name="jsws5", status="online")
    holder = User.objects.create(username="jh5", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.send_json_to(_inventory_frame())

        hc = _browser(holder, station.id)
        assert (await hc.connect())[0] is True
        await hc.send_json_to({"type": "lock_acquire"})
        await _until_lock_held(hc)

        # Agent drops.
        await agent.disconnect()

        # Browser must observe agent_offline and a free lock frame.
        offline = await _until(hc, "agent_offline")
        assert offline["type"] == "agent_offline"
        freed = await _until_lock_where(hc, lambda m: m.get("state") == "free")
        assert freed["state"] == "free"

        await hc.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 7. C1 — agent reconnect while browser socket stays open delivers a fresh
#    inventory frame.  Contract-level proof that the agentOffline latch is
#    clearable: the browser receives agent_offline on disconnect and then an
#    inventory on reconnect (the inventory ingestion clears agentOffline in
#    the JS component).
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_agent_reconnect_delivers_fresh_inventory(control_agent_auth):
    station = Station.objects.create(name="jsws6", status="online")
    viewer = User.objects.create(username="jv6", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        # First agent connects and the browser observes it.
        agent1 = _agent_comm(station.id)
        assert (await agent1.connect())[0] is True
        await agent1.send_json_to(_inventory_frame())

        hc = _browser(viewer, station.id)
        assert (await hc.connect())[0] is True
        # Drain the connect-time inventory + lock frames so the queue is clean.
        await _until(hc, "inventory")
        await _until(hc, "lock")

        # Agent drops — browser gets agent_offline.
        await agent1.disconnect()
        offline = await _until(hc, "agent_offline")
        assert offline["type"] == "agent_offline"

        # A NEW agent connects and sends a fresh inventory.
        agent2 = _agent_comm(station.id)
        assert (await agent2.connect())[0] is True
        await agent2.send_json_to(_inventory_frame())

        # The browser MUST receive that fresh inventory frame.  This is the
        # event that the JS _ingestInventory handler uses to clear agentOffline.
        fresh = await _until(hc, "inventory")
        assert fresh["type"] == "inventory"
        assert fresh["slots"][0]["slot"] == "slot0"

        await hc.disconnect()
        await agent2.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Regression: the §7 envelope version is mandatory on browser->server frames.
#
# The real station-agent parser (station_agent.protocol.parse_message) DROPS
# any frame whose "v" != PROTOCOL_VERSION. D5 shipped commands/subscribe/ptt
# WITHOUT the "v" field, so every relayed frame was silently rejected and the
# operator only ever saw a command timeout ("No response"). The Channels relay
# tests above missed it because the simulated agent receives raw JSON and never
# runs parse_message. This test pins the contract against the REAL parser.
# ---------------------------------------------------------------------------
def test_agent_parse_message_requires_envelope_version():
    from station_agent import protocol as proto

    # A command exactly as D5 builds it, WITH the envelope version -> accepted.
    good = json.dumps(
        {
            "v": proto.PROTOCOL_VERSION,
            "type": "command",
            "request_id": "b1-123",
            "slot": "slot0",
            "module": "fm0",
            "capability": "frequency",
            "op": "set",
            "value": 145.5,
        }
    )
    parsed = proto.parse_message(good)
    assert parsed["type"] == "command"

    # The SAME frame without "v" -> rejected (this was the production bug).
    bad = json.dumps(
        {
            "type": "command",
            "request_id": "b1-123",
            "slot": "slot0",
            "module": "fm0",
            "capability": "frequency",
            "op": "set",
            "value": 145.5,
        }
    )
    with pytest.raises(proto.ProtocolError) as exc:
        proto.parse_message(bad)
    assert exc.value.code == proto.VALIDATION_FAILED
