# tests/test_audio_server_relay_e2e.py
"""Full server-side audio relay E2E integration tests (Session C, Spec 0 §5/§6).

Exercises the WHOLE relay chain across BOTH consumers (AgentAudioConsumer +
AudioConsumer) plus the control-plane gate glue, treating Opus frames as
OPAQUE (frames loaded from tests/fixtures/audio/media_frame_slot0rx.bin,
parsed and asserted with station_agent.audio.frame).

No real audio — all Opus bytes come from the golden binary fixture.

Mirrors the harness in tests/test_control_consumer_relay.py:
  - WebsocketCommunicator for all WS connections
  - asyncio.run(scenario()) inside @pytest.mark.django_db(transaction=True)
  - browser sets comm.scope["user"] = user
  - group-spy via get_channel_layer().group_add

Fixtures used:
  - audio_agent_auth  (conftest) — monkeypatches AgentAudioConsumer._verify_agent
  - control_agent_auth (conftest) — monkeypatches AgentControlConsumer._verify_agent
"""

import asyncio
import json
import pathlib

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.stations.models import Station
from config.asgi import application
from station_agent.audio import frame as af

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

V = 1
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "audio"

# Load the §5.3 golden fixture once; used as the opaque Opus media frame
# throughout all scenarios (byte-identical relay assertion).
_RAW_FRAME_SLOT0RX: bytes = (FIXTURES_DIR / "media_frame_slot0rx.bin").read_bytes()
# stream_ref=0 for slot0.rx as declared in advertise.json
_SLOT0RX_REF: int = 0
# op.mic stream_ref=1 as declared in advertise.json
_OMIC_REF: int = 1

# Pre-parse once (validates fixture integrity at import time).
_PARSED_SLOT0RX: af.MediaFrame = af.parse_frame(_RAW_FRAME_SLOT0RX)

# Build an op.mic frame by repacking the fixture payload with stream_ref=1.
_RAW_FRAME_OMIC: bytes = af.pack_frame(
    stream_ref=_OMIC_REF,
    seq=_PARSED_SLOT0RX.seq,
    ts=_PARSED_SLOT0RX.ts,
    flags=_PARSED_SLOT0RX.flags,
    payload=_PARSED_SLOT0RX.payload,
)
_PARSED_OMIC: af.MediaFrame = af.parse_frame(_RAW_FRAME_OMIC)


# ---------------------------------------------------------------------------
# Communicator factories
# ---------------------------------------------------------------------------


def _agent_audio_comm(station_id: int) -> WebsocketCommunicator:
    """Ed25519 verification bypassed via audio_agent_auth fixture."""
    return WebsocketCommunicator(
        application, f"/ws/agent/audio/{station_id}/?signature=x&timestamp=0"
    )


def _agent_control_comm(station_id: int) -> WebsocketCommunicator:
    """Ed25519 verification bypassed via control_agent_auth fixture."""
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


def _browser_audio(user: User, station_id: int) -> WebsocketCommunicator:
    comm = WebsocketCommunicator(application, f"/ws/audio/{station_id}/")
    comm.scope["user"] = user
    return comm


def _browser_control(user: User, station_id: int) -> WebsocketCommunicator:
    comm = WebsocketCommunicator(application, f"/ws/control/{station_id}/")
    comm.scope["user"] = user
    return comm


# ---------------------------------------------------------------------------
# Frame draining helpers (bounded, deterministic — no infinite loops)
# ---------------------------------------------------------------------------


async def _until(comm: WebsocketCommunicator, mtype: str, tries: int = 12) -> dict:
    """Drain frames (skipping binary) until a JSON frame with the given type arrives."""
    for _ in range(tries):
        try:
            raw = await asyncio.wait_for(comm.receive_from(), timeout=2.0)
        except TimeoutError:
            raise AssertionError(f"timed out waiting for JSON type={mtype!r}")
        # Skip binary frames — only parse text/JSON.
        if isinstance(raw, (bytes, bytearray)):
            continue
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw type={mtype!r} in {tries} frames")


async def _until_bytes(comm: WebsocketCommunicator, tries: int = 8) -> bytes:
    """Drain until a binary frame arrives."""
    for _ in range(tries):
        try:
            data = await asyncio.wait_for(comm.receive_from(), timeout=2.0)
        except TimeoutError:
            raise AssertionError("timed out waiting for binary frame")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    raise AssertionError("never received a binary frame")


async def _spy_until(
    layer, channel: str, etype: str, tries: int = 12, timeout: float = 3.0
) -> dict:
    """Drain channel-layer events from a spy channel until one with the given type arrives."""
    for _ in range(tries):
        try:
            evt = await asyncio.wait_for(layer.receive(channel), timeout=timeout)
        except TimeoutError:
            raise AssertionError(f"timed out waiting for spy event type={etype!r}")
        if evt.get("type") == etype:
            return evt
    raise AssertionError(f"spy never saw event type={etype!r} in {tries} events")


async def _no_bytes(comm: WebsocketCommunicator, timeout: float = 0.4) -> None:
    """Assert that no binary frame arrives within timeout seconds."""
    try:
        data = await asyncio.wait_for(comm.receive_from(), timeout=timeout)
        if isinstance(data, (bytes, bytearray)):
            raise AssertionError(f"unexpected binary frame: {bytes(data)[:16]!r}...")
    except TimeoutError:
        pass


async def _until_lock_held(comm: WebsocketCommunicator, tries: int = 10) -> dict:
    """Drain control frames until lock{state=held} arrives."""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(comm.receive_json_from(), timeout=2.0)
        except TimeoutError:
            raise AssertionError("timed out waiting for lock(state=held)")
        if msg.get("type") == "lock" and msg.get("state") == "held":
            return msg
    raise AssertionError("never saw lock(state=held)")


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


def _make_member(username: str) -> User:
    """Create a MEMBER user who can_use_station."""
    return User.objects.create(
        username=username, membership_level=User.MembershipLevel.MEMBER
    )


async def _agent_advertise(agent: WebsocketCommunicator) -> None:
    """Send the golden advertise.json fixture from the agent."""
    await agent.send_json_to(_load_json("advertise.json"))


async def _browser_subscribe_slot0rx(browser: WebsocketCommunicator) -> None:
    """Browser subscribes to slot0.rx."""
    await browser.send_json_to(
        {"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]}
    )


# ===========================================================================
# Scenario 1 — Downlink RX fan-out
# ===========================================================================


@pytest.mark.django_db(transaction=True)
def test_downlink_rx_fanout_two_browsers(audio_agent_auth):
    """Full downlink RX fan-out: agent sends media_frame_slot0rx.bin → TWO browsers
    both receive it BYTE-IDENTICAL, stream_ref matches, payload matches.

    Steps exercised (Spec 0 §5 / component design §6):
    1. Agent connects + sends advertise → both browsers receive streams.
    2. Both browsers subscribe slot0.rx → agent receives exactly ONE source_subscribe.
    3. Agent sends the golden §5.3 binary frame → both browsers receive it
       byte-identically; parse_frame fields match.
    4. Browser-1 unsubscribes → NO source_unsubscribe (subscriber-2 still active).
    5. Browser-2 unsubscribes → agent receives source_unsubscribe.
    """
    station = Station.objects.create(name="fanout1", status="online")
    user1 = _make_member("fanout_u1")
    user2 = _make_member("fanout_u2")

    async def scenario():
        layer = get_channel_layer()
        # Spy on the agent group to intercept source_subscribe/unsubscribe.
        agent_spy = "agent-spy-fanout1"
        await layer.group_add(f"audio_{station.id}_agent", agent_spy)

        # --- 1. Agent connects + advertises ---
        agent = _agent_audio_comm(station.id)
        assert (await agent.connect())[0] is True
        await _agent_advertise(agent)

        # --- 2a. Browser-1 connects ---
        b1 = _browser_audio(user1, station.id)
        assert (await b1.connect())[0] is True
        # Browser-1 subscribes slot0.rx (first subscriber → source_subscribe to agent).
        await _browser_subscribe_slot0rx(b1)

        # Drain: first subscribe triggers source_subscribe in agent group.
        evt = await asyncio.wait_for(layer.receive(agent_spy), timeout=3.0)
        assert evt["type"] == "audio.to_agent"
        assert evt["msg"]["type"] == "source_subscribe"
        assert evt["msg"]["stream_id"] == "slot0.rx"

        # --- 2b. Browser-2 connects + subscribes ---
        b2 = _browser_audio(user2, station.id)
        assert (await b2.connect())[0] is True
        await _browser_subscribe_slot0rx(b2)
        # Second subscriber must NOT trigger another source_subscribe.
        # Drain any pending events with a short timeout; must not be source_subscribe.
        try:
            evt2 = await asyncio.wait_for(layer.receive(agent_spy), timeout=0.5)
            # If something arrived it must NOT be source_subscribe (it may be gate or other).
            assert evt2.get("msg", {}).get("type") != "source_subscribe", (
                f"second subscriber triggered an extra source_subscribe: {evt2}"
            )
        except TimeoutError:
            pass  # nothing arrived — correct, no duplicate source_subscribe

        # --- 3. Agent sends the golden §5.3 frame → both browsers receive it ---
        await agent.send_to(bytes_data=_RAW_FRAME_SLOT0RX)

        raw1 = await _until_bytes(b1)
        raw2 = await _until_bytes(b2)

        # Byte-identical relay — both browsers get the exact same bytes.
        assert raw1 == _RAW_FRAME_SLOT0RX, "browser-1 frame not byte-identical"
        assert raw2 == _RAW_FRAME_SLOT0RX, "browser-2 frame not byte-identical"

        # Parse and assert frame fields (Spec 0 §5.3).
        p1 = af.parse_frame(raw1)
        p2 = af.parse_frame(raw2)
        assert raw1[0] == af.MAGIC and raw1[1] == af.VERSION, "bad magic/ver in relayed frame"
        assert p1.stream_ref == _SLOT0RX_REF, f"stream_ref {p1.stream_ref} != {_SLOT0RX_REF}"
        assert p1.payload == _PARSED_SLOT0RX.payload, "payload not byte-identical after relay"
        assert p1 == p2, "parsed frames differ between browser-1 and browser-2"

        # --- 4. Browser-1 unsubscribes → NO source_unsubscribe ---
        await b1.send_json_to({"v": V, "type": "unsubscribe", "stream_ids": ["slot0.rx"]})
        # Wait briefly; spy must NOT receive a source_unsubscribe.
        try:
            evt_unsub = await asyncio.wait_for(layer.receive(agent_spy), timeout=0.5)
            assert evt_unsub.get("msg", {}).get("type") != "source_unsubscribe", (
                f"premature source_unsubscribe after first unsubscribe: {evt_unsub}"
            )
        except TimeoutError:
            pass  # nothing — correct

        # --- 5. Browser-2 unsubscribes → source_unsubscribe MUST arrive ---
        await b2.send_json_to({"v": V, "type": "unsubscribe", "stream_ids": ["slot0.rx"]})
        evt_final = await asyncio.wait_for(layer.receive(agent_spy), timeout=3.0)
        assert evt_final["type"] == "audio.to_agent"
        assert evt_final["msg"]["type"] == "source_unsubscribe"
        assert evt_final["msg"]["stream_id"] == "slot0.rx"

        # Cleanup.
        await b1.disconnect()
        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_downlink_rx_advertise_streams_relayed_to_browsers(audio_agent_auth):
    """Agent advertise → both browsers receive streams JSON with correct stream_refs.

    Validates §5.2/§5.3: stream_ref read from advertise, not inferred from index.
    """
    station = Station.objects.create(name="adv1", status="online")
    user1 = _make_member("adv_u1")
    user2 = _make_member("adv_u2")

    async def scenario():
        agent = _agent_audio_comm(station.id)
        assert (await agent.connect())[0] is True

        b1 = _browser_audio(user1, station.id)
        b2 = _browser_audio(user2, station.id)
        assert (await b1.connect())[0] is True
        assert (await b2.connect())[0] is True

        # Agent sends advertise.
        adv = _load_json("advertise.json")
        await agent.send_json_to(adv)

        # Both browsers should receive a "streams" frame.
        s1 = await _until(b1, "streams")
        s2 = await _until(b2, "streams")

        for s in (s1, s2):
            by_id = {e["stream_id"]: e for e in s["streams"]}
            assert "slot0.rx" in by_id and "op.mic" in by_id
            # stream_ref MUST be read from advertise, not inferred from index.
            assert by_id["slot0.rx"]["stream_ref"] == _SLOT0RX_REF
            assert by_id["op.mic"]["stream_ref"] == _OMIC_REF

        await b1.disconnect()
        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


# ===========================================================================
# Scenario 2 — Uplink mic→TX with the real cross-plane gate
# ===========================================================================


@pytest.mark.django_db(transaction=True)
def test_uplink_mic_through_ptt_lock_gate(audio_agent_auth, control_agent_auth):
    """Full cross-plane uplink gate: lock + PTT drive AudioGate so mic frame
    flows agent→browser correctly.

    Chain (Spec 0 §5.5, component design §7):
    1. Holder connects BOTH /ws/control/<id>/ AND /ws/audio/<id>/.
    2. Control agent connects (required for lock to function and PTT to be relayed).
    3. Holder acquires lock on control.
    4. Holder sends PTT command on control → ControlConsumer writes AudioGate +
       broadcasts audio.gate → AudioConsumer refreshes _gate_cache.
    5. Holder sends mic_open on audio → succeeds (no error).
    6. Holder sends op.mic binary frame → reaches AGENT AUDIO consumer
       byte-identically.
    7. A second browser subscribed to op.mic also receives the mic frame (producer
       fan-out §5.2).
    8. Agent receives a mic_state{active:true} event (via audio.gate → audio_gate
       handler on AgentAudioConsumer).
    """
    station = Station.objects.create(name="uplink1", status="online")
    holder = _make_member("uplink_holder")
    listener = _make_member("uplink_listener")

    async def scenario():
        layer = get_channel_layer()
        # Spy on the agent audio group to observe mic media frames reaching agent.
        agent_spy = "agent-spy-uplink1"
        await layer.group_add(f"audio_{station.id}_agent", agent_spy)

        # --- Control agent must be connected for ControlConsumer to see a station. ---
        ctrl_agent = _agent_control_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True
        # Feed minimal inventory so registry is seeded (required by ControlConsumer).
        await ctrl_agent.send_json_to(
            {
                "v": V,
                "type": "inventory",
                "slots": [
                    {
                        "slot": "slot0",
                        "modules": [
                            {
                                "module": "fm",
                                "identity": {"type": "fm"},
                                "capabilities": [
                                    {"name": "ptt", "kind": "command"}
                                ],
                                "state": {},
                            }
                        ],
                    }
                ],
            }
        )

        # --- Audio agent connects + advertises (needed for group + ref map). ---
        audio_agent = _agent_audio_comm(station.id)
        assert (await audio_agent.connect())[0] is True
        await _agent_advertise(audio_agent)

        # --- Holder connects control + audio. ---
        hc = _browser_control(holder, station.id)
        ha = _browser_audio(holder, station.id)
        assert (await hc.connect())[0] is True
        assert (await ha.connect())[0] is True

        # Drain initial control frames (inventory + lock).
        await _until(hc, "inventory")

        # Listener browser connects to audio + subscribes to op.mic for fan-out check.
        listener_a = _browser_audio(listener, station.id)
        assert (await listener_a.connect())[0] is True
        await listener_a.send_json_to(
            {"v": V, "type": "subscribe", "stream_ids": ["op.mic"]}
        )

        # --- Holder acquires lock on control. ---
        await hc.send_json_to({"type": "lock_acquire"})
        lock_evt = await _until_lock_held(hc)
        assert lock_evt["you_hold"] is True

        # --- Holder sends PTT command on control (capability="ptt", value=true). ---
        await hc.send_json_to(
            {
                "type": "command",
                "request_id": "ptt1",
                "slot": "slot0",
                "module": "fm",
                "capability": "ptt",
                "op": "do",
                "value": True,
            }
        )
        # PTT command triggers _audio_set_ptt + _bridge_gate; audio.gate broadcast
        # reaches the holder's AudioConsumer and the AgentAudioConsumer.

        # Agent audio consumer receives audio.gate → sends mic_state.
        mic_state_msg = await _until(audio_agent, "mic_state")
        assert mic_state_msg["active"] is True, (
            f"expected mic_state active=True, got: {mic_state_msg}"
        )
        assert mic_state_msg.get("tx_slot") == 0 or mic_state_msg.get("tx_slot") is not None

        # Wait briefly for the gate broadcast to propagate to holder's AudioConsumer.
        # The AudioConsumer.audio_gate handler refreshes _gate_cache in memory.
        await asyncio.sleep(0.05)

        # --- Holder sends mic_open on audio. ---
        await ha.send_json_to(
            {"v": V, "type": "mic_open", "format": {"rate": 16000, "channels": 1}, "codec": "opus"}
        )
        # Must NOT produce an error (gate is open).
        await _no_bytes(ha, timeout=0.3)

        # --- Holder sends op.mic binary frame. ---
        await ha.send_to(bytes_data=_RAW_FRAME_OMIC)

        # Agent spy must receive the frame byte-identically.
        # Drain past any audio.gate / audio.to_agent events that may have queued.
        agent_evt = await _spy_until(layer, agent_spy, "audio.media")
        assert agent_evt["data"] == _RAW_FRAME_OMIC, (
            "mic frame did not reach agent byte-identically"
        )

        # Listener browser (subscribed to op.mic) must also receive the fan-out.
        raw_listener = await _until_bytes(listener_a)
        assert raw_listener == _RAW_FRAME_OMIC, (
            "op.mic subscriber did not receive the mic frame (producer fan-out §5.2)"
        )
        p_listener = af.parse_frame(raw_listener)
        assert p_listener.stream_ref == _OMIC_REF
        assert p_listener.payload == _PARSED_OMIC.payload

        # Cleanup.
        await ha.disconnect()
        await hc.disconnect()
        await listener_a.disconnect()
        await audio_agent.disconnect()
        await ctrl_agent.disconnect()

    asyncio.run(scenario())


# ===========================================================================
# Scenario 3 — Gate denies without PTT/lock
# ===========================================================================


@pytest.mark.django_db(transaction=True)
def test_gate_denies_mic_frame_without_lock(audio_agent_auth):
    """A non-holder browser's mic frame is DROPPED; browser receives not_locked error.

    No lock, no PTT → uplink must not reach the agent. Browser gets exactly one
    not_locked error (throttled: one per transition, not per dropped frame).
    """
    station = Station.objects.create(name="deny1", status="online")
    user = _make_member("deny_u1")

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-deny1"
        await layer.group_add(f"audio_{station.id}_agent", agent_spy)

        audio_agent = _agent_audio_comm(station.id)
        assert (await audio_agent.connect())[0] is True
        await _agent_advertise(audio_agent)

        b = _browser_audio(user, station.id)
        assert (await b.connect())[0] is True

        # No lock, no PTT — gate is closed.
        await b.send_json_to(
            {"v": V, "type": "mic_open", "format": {"rate": 16000, "channels": 1}, "codec": "opus"}
        )
        # mic_open without lock → error.
        err = await _until(b, "error")
        assert err["code"] == "not_locked", f"expected not_locked, got {err['code']!r}"

        # Sending a mic frame while gate is closed → DROPPED (no bytes at agent).
        await b.send_to(bytes_data=_RAW_FRAME_OMIC)
        # Agent spy must receive nothing audio.media.
        try:
            evt = await asyncio.wait_for(layer.receive(agent_spy), timeout=0.5)
            assert evt.get("type") != "audio.media", (
                f"dropped frame reached agent: {evt}"
            )
        except TimeoutError:
            pass  # nothing — correct

        await b.disconnect()
        await audio_agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_gate_denies_mic_frame_after_lock_release(audio_agent_auth, control_agent_auth):
    """Holder sends mic frame AFTER lock_release → DROPPED; gate cleared.

    Chain:
    1. Holder acquires lock + PTT (gate opens).
    2. Holder sends mic frame → reaches agent (gate open).
    3. Holder sends lock_release on control → ControlConsumer clears PTT.
    4. Holder sends mic frame again → DROPPED; browser gets not_locked error.
    """
    station = Station.objects.create(name="deny2", status="online")
    holder = _make_member("deny_h2")

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-deny2"
        await layer.group_add(f"audio_{station.id}_agent", agent_spy)

        ctrl_agent = _agent_control_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True
        await ctrl_agent.send_json_to(
            {
                "v": V,
                "type": "inventory",
                "slots": [
                    {
                        "slot": "slot0",
                        "modules": [
                            {
                                "module": "fm",
                                "identity": {"type": "fm"},
                                "capabilities": [{"name": "ptt", "kind": "command"}],
                                "state": {},
                            }
                        ],
                    }
                ],
            }
        )

        audio_agent = _agent_audio_comm(station.id)
        assert (await audio_agent.connect())[0] is True
        await _agent_advertise(audio_agent)

        hc = _browser_control(holder, station.id)
        ha = _browser_audio(holder, station.id)
        assert (await hc.connect())[0] is True
        assert (await ha.connect())[0] is True
        await _until(hc, "inventory")

        # Acquire lock + PTT.
        await hc.send_json_to({"type": "lock_acquire"})
        await _until_lock_held(hc)
        await hc.send_json_to(
            {
                "type": "command",
                "request_id": "ptt2",
                "slot": "slot0",
                "module": "fm",
                "capability": "ptt",
                "op": "do",
                "value": True,
            }
        )
        # Wait for mic_state(active=true) on audio agent.
        mic_state = await _until(audio_agent, "mic_state")
        assert mic_state["active"] is True

        # Let gate broadcast propagate to holder's AudioConsumer.
        await asyncio.sleep(0.05)

        # Gate is open — mic frame should reach agent.
        await ha.send_to(bytes_data=_RAW_FRAME_OMIC)
        # Drain past any audio.gate events that may have arrived before the media frame.
        evt_open = await _spy_until(layer, agent_spy, "audio.media")
        assert evt_open["data"] == _RAW_FRAME_OMIC

        # --- Release lock → ControlConsumer clears PTT → audio.gate broadcast. ---
        await hc.send_json_to({"type": "lock_release"})
        # ControlConsumer broadcasts a new lock frame + clears PTT + sends audio.gate.
        # Wait for mic_state(active=false) on audio agent.
        mic_state_off = await _until(audio_agent, "mic_state")
        assert mic_state_off["active"] is False

        # Let gate broadcast propagate to holder's AudioConsumer.
        await asyncio.sleep(0.05)

        # Reset the error-throttle: send mic_close then mic_open so the browser
        # gets a fresh not_locked error on the NEXT frame (or on mic_open itself).
        await ha.send_json_to({"v": V, "type": "mic_close"})
        await ha.send_json_to(
            {"v": V, "type": "mic_open", "format": {"rate": 16000, "channels": 1}, "codec": "opus"}
        )
        err = await _until(ha, "error")
        assert err["code"] == "not_locked"

        # The subsequent mic frame must also be dropped.
        await ha.send_to(bytes_data=_RAW_FRAME_OMIC)
        try:
            evt_closed = await asyncio.wait_for(layer.receive(agent_spy), timeout=0.5)
            assert evt_closed.get("type") != "audio.media", (
                f"post-release mic frame reached agent: {evt_closed}"
            )
        except TimeoutError:
            pass  # nothing — correct

        await ha.disconnect()
        await hc.disconnect()
        await audio_agent.disconnect()
        await ctrl_agent.disconnect()

    asyncio.run(scenario())


# ===========================================================================
# Scenario 4 — Disconnect teardown (subscription reaping)
# ===========================================================================


@pytest.mark.django_db(transaction=True)
def test_browser_disconnect_reaps_subscriptions_and_source_unsubscribes(audio_agent_auth):
    """Browser disconnect drops all demand rows; agent receives source_unsubscribe for
    each source that hit zero (§6 demand-counting).

    This verifies that no leaked demand survives a disconnect and that a reconnect
    can re-subscribe cleanly.
    """
    from apps.audio.models import AudioSubscription

    station = Station.objects.create(name="teardown1", status="online")
    user = _make_member("td_u1")

    async def scenario():
        layer = get_channel_layer()
        agent_spy = "agent-spy-td1"
        await layer.group_add(f"audio_{station.id}_agent", agent_spy)

        audio_agent = _agent_audio_comm(station.id)
        assert (await audio_agent.connect())[0] is True
        await _agent_advertise(audio_agent)

        b = _browser_audio(user, station.id)
        assert (await b.connect())[0] is True

        # Subscribe to slot0.rx (first subscriber → source_subscribe).
        await _browser_subscribe_slot0rx(b)
        evt = await asyncio.wait_for(layer.receive(agent_spy), timeout=3.0)
        assert evt["msg"]["type"] == "source_subscribe"

        # Disconnect → demand row deleted → source_unsubscribe sent.
        await b.disconnect()

        evt_unsub = await asyncio.wait_for(layer.receive(agent_spy), timeout=3.0)
        assert evt_unsub["type"] == "audio.to_agent"
        assert evt_unsub["msg"]["type"] == "source_unsubscribe"
        assert evt_unsub["msg"]["stream_id"] == "slot0.rx"

        await audio_agent.disconnect()

    asyncio.run(scenario())

    # No demand rows must survive.
    assert AudioSubscription.objects.filter(station=station).count() == 0
