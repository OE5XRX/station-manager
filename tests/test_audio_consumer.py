"""Channels consumer tests for the audio relay (Session C).

No pytest-asyncio — async scenarios run via asyncio.run() inside
@pytest.mark.django_db(transaction=True), mirroring test_control_consumer_relay.py.
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

V = 1

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "audio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_comm(station_id):
    """Ed25519 verification bypassed via audio_agent_auth fixture."""
    return WebsocketCommunicator(
        application, f"/ws/agent/audio/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/audio/{station_id}/")
    comm.scope["user"] = user
    return comm


def _load_json(name):
    return json.loads((FIXTURES_DIR / name).read_text())


def _load_bin(name):
    return (FIXTURES_DIR / name).read_bytes()


async def _drain_until(comm, mtype, tries=10):
    """Drain frames from comm until one with the given type arrives."""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(comm.receive_json_from(), timeout=2.0)
        except TimeoutError:
            raise AssertionError(f"timed out waiting for type={mtype!r}")
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"never saw type={mtype!r} in {tries} frames")


async def _drain_bytes_until(comm, tries=5):
    """Drain until we receive a binary frame (bytes, not str)."""
    for _ in range(tries):
        data = await asyncio.wait_for(comm.receive_from(), timeout=2.0)
        if isinstance(data, bytes):
            return data
    raise AssertionError("never received a binary frame")


async def _no_bytes(comm, timeout=0.3):
    """Assert no binary frame arrives within timeout.

    Also fails if a JSON (text) frame arrives — a text frame where silence is
    expected is equally surprising and should surface immediately.
    """
    try:
        data = await asyncio.wait_for(comm.receive_from(), timeout=timeout)
        if isinstance(data, bytes):
            raise AssertionError(f"unexpected binary frame: {data[:16]!r}...")
        # A text frame arriving when we expected silence is also a failure.
        raise AssertionError(f"unexpected text frame when expecting silence: {data!r}")
    except TimeoutError:
        pass


async def _no_json(comm, timeout=0.3):
    """Assert no JSON frame arrives within timeout.

    Also fails if a binary frame arrives — binary where silence is expected is
    equally surprising.
    """
    try:
        data = await asyncio.wait_for(comm.receive_from(), timeout=timeout)
        if isinstance(data, str):
            msg = json.loads(data)
            raise AssertionError(f"unexpected JSON frame: {msg}")
        # A binary frame arriving when we expected silence is also a failure.
        raise AssertionError(f"unexpected binary frame when expecting silence: {data[:16]!r}...")
    except TimeoutError:
        pass


# ---------------------------------------------------------------------------
# 0. gate.py unit tests (DB mic_allowed + msgpack-safe wire state)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gate_mic_allowed_db_check():
    """Direct DB unit test for gate.mic_allowed (holder + active PTT + not expired)."""
    from apps.audio import gate as audio_gate
    from apps.control import lock as control_lock

    station = Station.objects.create(name="gate-mic", status="online")
    holder = User.objects.create(
        username="gate_holder", membership_level=User.MembershipLevel.MEMBER
    )
    other = User.objects.create(
        username="gate_other", membership_level=User.MembershipLevel.MEMBER
    )

    # No lock, no PTT → not allowed.
    assert audio_gate.mic_allowed(station, holder) is False

    control_lock.acquire(station, holder)
    # Lock but no PTT → not allowed.
    assert audio_gate.mic_allowed(station, holder) is False

    audio_gate.set_ptt(station, slot=0, module="fm")
    # Holder + active PTT → allowed; non-holder → not.
    assert audio_gate.mic_allowed(station, holder) is True
    assert audio_gate.mic_allowed(station, other) is False

    # Clearing PTT closes the gate again.
    audio_gate.clear_ptt(station)
    assert audio_gate.mic_allowed(station, holder) is False


@pytest.mark.django_db(transaction=True)
def test_gate_wire_state_is_msgpack_safe():
    """gate.get_wire_state returns only primitives (epoch float, holder_id) — msgpack-safe."""
    import msgpack

    from apps.audio import gate as audio_gate
    from apps.control import lock as control_lock

    station = Station.objects.create(name="gate-wire", status="online")
    holder = User.objects.create(
        username="wire_holder", membership_level=User.MembershipLevel.MEMBER
    )
    control_lock.acquire(station, holder)
    audio_gate.set_ptt(station, slot=0, module="fm")

    state = audio_gate.get_wire_state(station)
    # No datetime; epoch is a float; holder_id propagated.
    assert "ptt_expires_at" not in state
    assert isinstance(state["ptt_expires_epoch"], float)
    assert state["holder_id"] == holder.id
    assert state["ptt_active"] is True
    # Round-trips through msgpack (the prod channels_redis layer).
    assert isinstance(msgpack.packb(state), (bytes, bytearray))

    # Empty gate (no row): still msgpack-safe, holder_id None when no lock.
    empty_station = Station.objects.create(name="gate-empty", status="online")
    empty = audio_gate.get_wire_state(empty_station)
    assert empty["ptt_expires_epoch"] is None
    assert empty["holder_id"] is None
    assert isinstance(msgpack.packb(empty), (bytes, bytearray))


# ---------------------------------------------------------------------------
# 1. Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_agent_auth_accept(audio_agent_auth):
    """Monkeypatched verify → agent connects successfully."""
    station = Station.objects.create(name="a1", status="online")

    async def scenario():
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_agent_auth_reject_unknown_station():
    """Unknown station_id → pre-accept close 4404 (no monkeypatch)."""

    async def scenario():
        agent = WebsocketCommunicator(
            application, "/ws/agent/audio/999999/?signature=x&timestamp=0"
        )
        connected, code = await agent.connect()
        assert connected is False
        assert code == 4404

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_agent_auth_reject_bad_sig():
    """Bad signature → close 4401."""
    from apps.api.models import DeviceKey

    station = Station.objects.create(name="a2", status="online")
    DeviceKey.objects.create(
        station=station,
        current_public_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        is_active=True,
    )

    async def scenario():
        # timestamp=0 is stale by >60s → rejected
        agent = WebsocketCommunicator(
            application, f"/ws/agent/audio/{station.id}/?signature=x&timestamp=0"
        )
        connected, code = await agent.connect()
        assert connected is False
        assert code == 4401

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_agent_real_ed25519_accept():
    """Real Ed25519 keypair — correct sig → accept; tampered sig → reject."""
    import base64
    import hashlib
    import time

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from apps.api.models import DeviceKey

    station = Station.objects.create(name="a3_ed25519", status="online")
    private_key = Ed25519PrivateKey.generate()
    pub_raw = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    DeviceKey.objects.create(
        station=station, current_public_key=pub_b64, is_active=True
    )

    def _make_qs(timestamp_str, priv_key):
        from urllib.parse import quote

        body_hash = hashlib.sha256(b"").hexdigest()
        signed_data = f"{timestamp_str}:{body_hash}".encode()
        sig = priv_key.sign(signed_data)
        sig_b64 = base64.b64encode(sig).decode("ascii")
        # URL-encode so parse_qs decodes it correctly (+ and / need escaping).
        sig_enc = quote(sig_b64, safe="")
        return f"/ws/agent/audio/{station.id}/?signature={sig_enc}&timestamp={timestamp_str}"

    async def scenario():
        ts = str(time.time())
        # Correct sig → accept
        qs = _make_qs(ts, private_key)
        agent = WebsocketCommunicator(application, qs)
        connected, _ = await agent.connect()
        assert connected is True
        await agent.disconnect()

        # Tampered sig → reject
        tampered_qs = _make_qs(ts, Ed25519PrivateKey.generate())
        agent2 = WebsocketCommunicator(application, tampered_qs)
        connected2, code2 = await agent2.connect()
        assert connected2 is False
        assert code2 == 4401

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_browser_anon_reject():
    """Anonymous user → 4401 (accept-then-error)."""
    from django.contrib.auth.models import AnonymousUser

    station = Station.objects.create(name="b1", status="online")

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/audio/{station.id}/")
        comm.scope["user"] = AnonymousUser()
        connected, _ = await comm.connect()
        assert connected is True  # accept-then-error pattern
        err = await _drain_until(comm, "error")
        assert err["code"] == "4401"
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_browser_no_permission_reject():
    """Applicant can_use_station=False → 4403."""
    station = Station.objects.create(name="b2", status="online")
    user = User.objects.create(
        username="applicant_b2", membership_level=User.MembershipLevel.APPLICANT
    )

    async def scenario():
        comm = _browser(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        err = await _drain_until(comm, "error")
        assert err["code"] == "4403"
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_browser_unknown_station_reject():
    """Unknown station_id → 4404."""
    user = User.objects.create(
        username="member_b3", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        comm = WebsocketCommunicator(application, "/ws/audio/999999/")
        comm.scope["user"] = user
        connected, _ = await comm.connect()
        assert connected is True
        err = await _drain_until(comm, "error")
        assert err["code"] == "4404"
        await comm.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_browser_valid_accept():
    """Valid member → successful connect."""
    station = Station.objects.create(name="b4", status="online")
    user = User.objects.create(
        username="member_b4", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        comm = _browser(user, station.id)
        connected, _ = await comm.connect()
        assert connected is True
        await comm.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. advertise → streams relay
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_advertise_relays_streams_to_browser(audio_agent_auth):
    """Agent sends advertise.json → browser sees streams with correct stream_refs."""
    station = Station.objects.create(name="c1", status="online")
    user = User.objects.create(
        username="member_c1", membership_level=User.MembershipLevel.MEMBER
    )
    advertise = _load_json("advertise.json")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        # Let the browser connect() finish joining browser_group (after its
        # post-accept DB round-trips) before the agent broadcasts.
        await asyncio.sleep(0.1)

        await agent.send_json_to(advertise)

        msg = await _drain_until(browser, "streams")
        streams = msg["streams"]
        assert len(streams) == 2

        slot0 = next(s for s in streams if s["stream_id"] == "slot0.rx")
        omic = next(s for s in streams if s["stream_id"] == "op.mic")

        # stream_ref MUST come from advertise explicitly, not inferred from index.
        assert slot0["stream_ref"] == 0
        assert omic["stream_ref"] == 1

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. Demand gating + fan-out
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_subscribe_sends_source_subscribe_once(audio_agent_auth):
    """Two browsers subscribe to same stream → agent receives ONE source_subscribe."""
    from apps.audio.models import AudioSubscription

    station = Station.objects.create(name="d1", status="online")
    user1 = User.objects.create(
        username="u1_d1", membership_level=User.MembershipLevel.MEMBER
    )
    user2 = User.objects.create(
        username="u2_d1", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        b1 = _browser(user1, station.id)
        b2 = _browser(user2, station.id)
        assert (await b1.connect())[0] is True
        assert (await b2.connect())[0] is True

        # First browser subscribes → expect source_subscribe on agent.
        await b1.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        ss = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert ss["type"] == "source_subscribe"
        assert ss["stream_id"] == "slot0.rx"

        # Second browser subscribes → NO second source_subscribe should arrive.
        await b2.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})

        # Give the consumer a moment and then check no further source_subscribe arrives.
        try:
            extra = await asyncio.wait_for(agent.receive_json_from(), timeout=0.3)
            # If something arrived, it must NOT be a second source_subscribe for slot0.rx
            assert not (
                extra.get("type") == "source_subscribe"
                and extra.get("stream_id") == "slot0.rx"
            ), f"unexpected second source_subscribe: {extra}"
        except TimeoutError:
            pass

        await b1.disconnect()
        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())

    # Verify demand rows are gone after disconnect.
    assert AudioSubscription.objects.filter(station=station, stream_id="slot0.rx").count() == 0


@pytest.mark.django_db(transaction=True)
def test_media_fan_out_to_two_browsers(audio_agent_auth):
    """Agent sends binary frame → both subscribing browsers receive byte-identical bytes."""
    from station_agent.audio.frame import parse_frame

    station = Station.objects.create(name="d2", status="online")
    user1 = User.objects.create(
        username="u1_d2", membership_level=User.MembershipLevel.MEMBER
    )
    user2 = User.objects.create(
        username="u2_d2", membership_level=User.MembershipLevel.MEMBER
    )
    advertise = _load_json("advertise.json")
    media_frame = _load_bin("media_frame_slot0rx.bin")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        b1 = _browser(user1, station.id)
        b2 = _browser(user2, station.id)
        assert (await b1.connect())[0] is True
        assert (await b2.connect())[0] is True
        # Let both browsers finish joining browser_group before broadcasting.
        await asyncio.sleep(0.1)

        # Agent advertises so stream_refs map is populated.
        await agent.send_json_to(advertise)
        # Both browsers receive streams broadcast.
        await _drain_until(b1, "streams")
        await _drain_until(b2, "streams")

        # Both browsers subscribe to slot0.rx.
        await b1.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        # First source_subscribe arrives at agent.
        await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)

        await b2.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        # Allow time for b2's subscribe handler to complete the DB round-trip
        # and group_add before the agent sends the media frame.
        await asyncio.sleep(0.1)

        # Agent sends one binary media frame.
        await agent.send_to(bytes_data=media_frame)

        # Both browsers receive byte-identical frames.
        frame1 = await _drain_bytes_until(b1)
        frame2 = await _drain_bytes_until(b2)

        assert frame1 == media_frame, "browser1 frame not byte-identical to source"
        assert frame2 == media_frame, "browser2 frame not byte-identical to source"

        # Parse and verify header fields.
        parsed = parse_frame(media_frame)
        assert parsed.stream_ref == 0  # slot0.rx ref from advertise.json

        parsed1 = parse_frame(frame1)
        assert parsed1.stream_ref == parsed.stream_ref
        assert parsed1.seq == parsed.seq

        await b1.disconnect()
        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_source_unsubscribe_at_zero(audio_agent_auth):
    """Two browsers → both unsubscribe → agent receives source_unsubscribe exactly once."""
    station = Station.objects.create(name="d3", status="online")
    user1 = User.objects.create(
        username="u1_d3", membership_level=User.MembershipLevel.MEMBER
    )
    user2 = User.objects.create(
        username="u2_d3", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        b1 = _browser(user1, station.id)
        b2 = _browser(user2, station.id)
        assert (await b1.connect())[0] is True
        assert (await b2.connect())[0] is True

        # Both subscribe.
        await b1.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        sub1 = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert sub1["type"] == "source_subscribe"

        await b2.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        # No second source_subscribe expected.

        # First browser unsubscribes — demand still 1 → no source_unsubscribe yet.
        await b1.send_json_to({"v": V, "type": "unsubscribe", "stream_ids": ["slot0.rx"]})
        try:
            msg = await asyncio.wait_for(agent.receive_json_from(), timeout=0.3)
            assert not (
                msg.get("type") == "source_unsubscribe"
                and msg.get("stream_id") == "slot0.rx"
            ), "premature source_unsubscribe"
        except TimeoutError:
            pass

        # Second browser unsubscribes — demand hits 0 → source_unsubscribe.
        await b2.send_json_to({"v": V, "type": "unsubscribe", "stream_ids": ["slot0.rx"]})
        unsub = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert unsub["type"] == "source_unsubscribe"
        assert unsub["stream_id"] == "slot0.rx"

        await b1.disconnect()
        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Uplink gating
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_uplink_no_lock_drops_frame_and_errors(audio_agent_auth):
    """Browser without lock sends mic frame → dropped + error not_locked, agent gets nothing."""
    from station_agent.audio.frame import pack_frame

    station = Station.objects.create(name="e1", status="online")
    user = User.objects.create(
        username="member_e1", membership_level=User.MembershipLevel.MEMBER
    )
    mic_frame = pack_frame(stream_ref=1, seq=0, ts=0, flags=0, payload=b"\x01\x02")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        layer = get_channel_layer()
        agent_spy = "agent_spy_e1"
        from apps.audio.constants import agent_group
        await layer.group_add(agent_group(station.id), agent_spy)

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True

        # Send mic_open (no lock → error).
        await browser.send_json_to(
            {"v": V, "type": "mic_open", "format": {"rate": 16000, "channels": 1}, "codec": "opus"}
        )
        err = await _drain_until(browser, "error")
        assert err["code"] == "not_locked"

        # Now send a binary mic frame → should be dropped.
        await browser.send_to(bytes_data=mic_frame)
        # Agent must not receive the binary frame.
        await _no_bytes(agent)

        # Remove spy from group.
        await layer.group_discard(agent_group(station.id), agent_spy)

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_uplink_lock_ptt_tx_relays_to_agent(audio_agent_auth):
    """Browser with lock + PTT + tx_route sends mic frame → relayed to agent + op.mic fans."""
    from apps.audio import gate as audio_gate
    from apps.control import lock as control_lock
    from station_agent.audio.frame import pack_frame, parse_frame

    station = Station.objects.create(name="e2", status="online")
    user = User.objects.create(
        username="member_e2", membership_level=User.MembershipLevel.MEMBER
    )
    # Also subscribe a second browser to op.mic to verify fan-out.
    listener = User.objects.create(
        username="listener_e2", membership_level=User.MembershipLevel.MEMBER
    )

    mic_frame = pack_frame(stream_ref=1, seq=0, ts=0, flags=0, payload=b"\x01\x02\x03")

    # Set up lock + PTT + tx_route BEFORE async context.
    control_lock.acquire(station, user)
    audio_gate.set_ptt(station, slot=0, module="fm")
    audio_gate.set_tx_route(station, slot=0, module="fm")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True

        b_listener = _browser(listener, station.id)
        assert (await b_listener.connect())[0] is True

        # Listener subscribes to op.mic so fan-out is exercised.
        await b_listener.send_json_to(
            {"v": V, "type": "subscribe", "stream_ids": ["op.mic"]}
        )
        # Agent receives source_subscribe for op.mic.
        sub = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert sub["type"] == "source_subscribe"
        assert sub["stream_id"] == "op.mic"

        # Browser sends mic frame → should relay.
        await browser.send_to(bytes_data=mic_frame)

        # Agent receives byte-identical frame.
        agent_frame = await _drain_bytes_until(agent)
        assert agent_frame == mic_frame, "agent frame not byte-identical"
        assert parse_frame(agent_frame).stream_ref == 1

        # Listener (subscribed to op.mic) also receives it.
        listener_frame = await _drain_bytes_until(b_listener)
        assert listener_frame == mic_frame, "listener frame not byte-identical"

        await b_listener.disconnect()
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_uplink_lock_no_ptt_drops(audio_agent_auth):
    """Browser holds lock but PTT not active → frame dropped + error."""
    from apps.control import lock as control_lock
    from station_agent.audio.frame import pack_frame

    station = Station.objects.create(name="e3", status="online")
    user = User.objects.create(
        username="member_e3", membership_level=User.MembershipLevel.MEMBER
    )
    mic_frame = pack_frame(stream_ref=1, seq=0, ts=0, flags=0, payload=b"\x01")

    # Acquire lock BEFORE entering async context (DB ops must be sync here).
    control_lock.acquire(station, user)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True

        await browser.send_to(bytes_data=mic_frame)
        # Agent should NOT receive binary.
        await _no_bytes(agent)
        # Browser gets not_locked error.
        err = await _drain_until(browser, "error")
        assert err["code"] == "not_locked"

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_mic_state_sent_to_agent_on_gate_change(audio_agent_auth):
    """When gate changes via audio.gate broadcast, agent receives mic_state."""
    from apps.audio import constants as audio_constants
    from apps.audio import gate as audio_gate

    station = Station.objects.create(name="e4", status="online")

    # Set gate state BEFORE entering async context.
    audio_gate.set_ptt(station, slot=0, module="fm")
    audio_gate.set_tx_route(station, slot=0, module="fm")
    # Broadcasts must carry the msgpack-safe wire state (no datetime).
    gate_state = audio_gate.get_wire_state(station)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        layer = get_channel_layer()
        state = gate_state

        # Manually broadcast audio.gate as the control consumer would.
        await layer.group_send(
            audio_constants.agent_group(station.id),
            {"type": "audio.gate", "state": state},
        )

        # Agent consumer's audio_gate handler should send mic_state.
        mic_state = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert mic_state["type"] == "mic_state"
        assert mic_state["active"] is True
        assert mic_state["tx_slot"] == 0
        assert mic_state["tx_module"] == "fm"

        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_mic_state_tx_route_fallback_to_ptt_module(audio_agent_auth):
    """PTT on slot0 with NO tx_route → agent still gets mic_state{active, tx_slot:0}.

    Without an explicit tx_route command the gate's tx_slot/tx_module stay unset;
    the agent-side mic_state must fall back to the PTT'd module so injection works.
    """
    from apps.audio import constants as audio_constants
    from apps.audio import gate as audio_gate

    station = Station.objects.create(name="e5", status="online")

    # PTT keyed on slot0/fm but NO tx_route ever set.
    audio_gate.set_ptt(station, slot=0, module="fm")
    gate_state = audio_gate.get_wire_state(station)
    # Sanity: tx_route really is unset in the wire state.
    assert gate_state["tx_slot"] is None
    assert gate_state["tx_module"] == ""
    assert gate_state["ptt_slot"] == 0

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        layer = get_channel_layer()
        await layer.group_send(
            audio_constants.agent_group(station.id),
            {"type": "audio.gate", "state": gate_state},
        )

        mic_state = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert mic_state["type"] == "mic_state"
        # Fallback: active True, tx target derived from the PTT'd module.
        assert mic_state["active"] is True
        assert mic_state["tx_slot"] == 0
        assert mic_state["tx_module"] == "fm"

        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Byte-identical passthrough
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_byte_identical_passthrough(audio_agent_auth):
    """Explicit assertion: emitted bytes == source bytes from fixture."""
    from station_agent.audio.frame import parse_frame

    station = Station.objects.create(name="f1", status="online")
    user = User.objects.create(
        username="member_f1", membership_level=User.MembershipLevel.MEMBER
    )
    advertise = _load_json("advertise.json")
    media_frame = _load_bin("media_frame_slot0rx.bin")
    expected = parse_frame(media_frame)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        # Let the browser finish joining browser_group before broadcasting.
        await asyncio.sleep(0.1)

        await agent.send_json_to(advertise)
        await _drain_until(browser, "streams")

        await browser.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)

        await agent.send_to(bytes_data=media_frame)
        received = await _drain_bytes_until(browser)

        # Byte identity.
        assert received == media_frame
        # Parsed fields match.
        got = parse_frame(received)
        assert got.stream_ref == expected.stream_ref
        assert got.seq == expected.seq
        assert got.ts == expected.ts
        assert got.flags == expected.flags
        assert got.payload == expected.payload

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. Reconnect
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_agent_disconnect_clears_ptt(audio_agent_auth):
    """Agent disconnect → gate PTT cleared."""
    from apps.audio import gate as audio_gate
    from apps.audio.models import AudioGate

    station = Station.objects.create(name="g1", status="online")
    audio_gate.set_ptt(station, slot=0, module="fm")

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        await agent.disconnect()

    asyncio.run(scenario())

    gate_row = AudioGate.objects.filter(station=station).first()
    # After disconnect, PTT must be cleared.
    assert gate_row is None or gate_row.ptt_active is False


@pytest.mark.django_db(transaction=True)
def test_browser_reconnect_no_leaked_demand(audio_agent_auth):
    """Browser disconnect + reconnect leaves no stale AudioSubscription rows."""
    from apps.audio.models import AudioSubscription

    station = Station.objects.create(name="g2", status="online")
    user = User.objects.create(
        username="member_g2", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        # First connection + subscribe.
        b1 = _browser(user, station.id)
        assert (await b1.connect())[0] is True
        await b1.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)

        await b1.disconnect()
        # source_unsubscribe expected.
        unsub = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert unsub["type"] == "source_unsubscribe"

        # Check no leaked demand rows (must use sync_to_async in async context).
        from channels.db import database_sync_to_async

        sub_count = await database_sync_to_async(
            lambda: AudioSubscription.objects.filter(station=station, stream_id="slot0.rx").count()
        )()
        assert sub_count == 0

        # Reconnect — clean slate.
        b2 = _browser(user, station.id)
        assert (await b2.connect())[0] is True
        await b2.send_json_to({"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]})
        sub2 = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert sub2["type"] == "source_subscribe"

        await b2.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())

    # All demand cleaned up.
    assert AudioSubscription.objects.filter(station=station).count() == 0


@pytest.mark.django_db(transaction=True)
def test_second_agent_connect_works_after_first_disconnect(audio_agent_auth):
    """Agent reconnect after disconnect → new connection accepted."""
    station = Station.objects.create(name="g3", status="online")

    async def scenario():
        a1 = _agent_comm(station.id)
        assert (await a1.connect())[0] is True
        await a1.disconnect()

        a2 = _agent_comm(station.id)
        connected, _ = await a2.connect()
        assert connected is True
        await a2.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 7. Control-plane glue
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_control_tx_route_updates_gate(control_agent_auth, audio_agent_auth):
    """tx_route command on /ws/control (lock held) → AudioGate.tx_slot updates."""
    from apps.audio.models import AudioGate

    station = Station.objects.create(name="h1", status="online")
    holder = User.objects.create(
        username="holder_h1", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        # Control agent (to satisfy lock relay path) — not strictly required for gate,
        # but mirroring real flow.
        from tests.test_control_consumer_relay import _agent_comm as ctrl_agent_comm

        ctrl_agent = ctrl_agent_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True

        hc = WebsocketCommunicator(application, f"/ws/control/{station.id}/")
        hc.scope["user"] = holder
        assert (await hc.connect())[0] is True

        # Skip initial inventory + lock frames.
        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # inventory
        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # lock

        # Acquire lock.
        await hc.send_json_to({"type": "lock_acquire"})
        while True:
            msg = await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)
            if msg.get("type") == "lock" and msg.get("state") == "held":
                break

        # Send tx_route command.
        await hc.send_json_to({
            "type": "command",
            "capability": "tx_route",
            "op": "set",
            "slot": 1000,
            "module": "audio-router",
            "value": {"slot": 0, "module": "fm"},
        })
        # Drain agent frame (relayed command).
        await asyncio.wait_for(ctrl_agent.receive_json_from(), timeout=2.0)

        # Give a moment for async DB write.
        await asyncio.sleep(0.1)

        await hc.disconnect()
        await ctrl_agent.disconnect()

    asyncio.run(scenario())

    gate = AudioGate.objects.filter(station=station).first()
    assert gate is not None
    assert gate.tx_slot == 0
    assert gate.tx_module == "fm"


@pytest.mark.django_db(transaction=True)
def test_audio_gate_broadcast_is_msgpack_serializable(control_agent_auth, audio_agent_auth):
    """REGRESSION: the audio.gate broadcast payload must be msgpack-safe.

    Prod uses channels_redis (msgpack) which cannot serialize datetime.  A raw
    datetime in the payload would crash group_send in prod (the InMemory test
    layer skips serialization, hiding the bug).  Capture the real audio.gate
    event emitted after a PTT command and assert msgpack.packb does not raise.
    """
    import msgpack

    from apps.audio.constants import browser_group

    station = Station.objects.create(name="h1b", status="online")
    holder = User.objects.create(
        username="holder_h1b", membership_level=User.MembershipLevel.MEMBER
    )

    captured = {}

    async def scenario():
        from tests.test_control_consumer_relay import _agent_comm as ctrl_agent_comm

        layer = get_channel_layer()
        # Spy on the browser audio group to capture the audio.gate broadcast.
        gate_spy = "audio-gate-spy-h1b"
        await layer.group_add(browser_group(station.id), gate_spy)

        ctrl_agent = ctrl_agent_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True

        hc = WebsocketCommunicator(application, f"/ws/control/{station.id}/")
        hc.scope["user"] = holder
        assert (await hc.connect())[0] is True

        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # inventory
        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # lock

        await hc.send_json_to({"type": "lock_acquire"})
        while True:
            msg = await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)
            if msg.get("type") == "lock" and msg.get("state") == "held":
                break

        # PTT on → the control glue broadcasts audio.gate to browser_group.
        await hc.send_json_to({
            "type": "command",
            "capability": "ptt",
            "op": "do",
            "value": True,
            "slot": 0,
            "module": "fm",
        })
        # Drain the relayed command from the agent.
        await asyncio.wait_for(ctrl_agent.receive_json_from(), timeout=2.0)

        # Capture the audio.gate event on the spy.
        for _ in range(10):
            evt = await asyncio.wait_for(layer.receive(gate_spy), timeout=2.0)
            if evt.get("type") == "audio.gate":
                captured["payload"] = evt
                break
        assert "payload" in captured, "never saw an audio.gate broadcast"

        await hc.disconnect()
        await ctrl_agent.disconnect()
        await layer.group_discard(browser_group(station.id), gate_spy)

    asyncio.run(scenario())

    payload = captured["payload"]
    # The gate state must carry an epoch float, not a datetime.
    state = payload["state"]
    assert "ptt_expires_epoch" in state
    assert "ptt_expires_at" not in state
    exp = state["ptt_expires_epoch"]
    assert exp is None or isinstance(exp, float)
    # The whole broadcast payload must round-trip through msgpack (prod layer).
    packed = msgpack.packb(payload)
    assert isinstance(packed, (bytes, bytearray))
    unpacked = msgpack.unpackb(packed, raw=False)
    assert unpacked["type"] == "audio.gate"
    assert unpacked["state"]["ptt_active"] is True
    assert unpacked["state"]["holder_id"] == holder.id


@pytest.mark.django_db(transaction=True)
def test_control_ptt_on_off_updates_gate(control_agent_auth, audio_agent_auth):
    """PTT on → gate.ptt_active=True; PTT off → False; lock_release → also False."""
    from apps.audio.models import AudioGate

    station = Station.objects.create(name="h2", status="online")
    holder = User.objects.create(
        username="holder_h2", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        from tests.test_control_consumer_relay import _agent_comm as ctrl_agent_comm

        ctrl_agent = ctrl_agent_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True

        hc = WebsocketCommunicator(application, f"/ws/control/{station.id}/")
        hc.scope["user"] = holder
        assert (await hc.connect())[0] is True

        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # inventory
        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # lock

        # Acquire lock.
        await hc.send_json_to({"type": "lock_acquire"})
        while True:
            msg = await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)
            if msg.get("type") == "lock" and msg.get("state") == "held":
                break

        # PTT on.
        await hc.send_json_to({
            "type": "command",
            "capability": "ptt",
            "op": "do",
            "value": True,
            "slot": 0,
            "module": "fm",
        })
        await asyncio.wait_for(ctrl_agent.receive_json_from(), timeout=2.0)
        await asyncio.sleep(0.1)

        # PTT off.
        await hc.send_json_to({
            "type": "command",
            "capability": "ptt",
            "op": "do",
            "value": False,
            "slot": 0,
            "module": "fm",
        })
        await asyncio.wait_for(ctrl_agent.receive_json_from(), timeout=2.0)
        await asyncio.sleep(0.1)

        # PTT back on.
        await hc.send_json_to({
            "type": "command",
            "capability": "ptt",
            "op": "do",
            "value": True,
            "slot": 0,
            "module": "fm",
        })
        await asyncio.wait_for(ctrl_agent.receive_json_from(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Lock release → PTT cleared.
        await hc.send_json_to({"type": "lock_release"})
        await asyncio.sleep(0.2)

        await hc.disconnect()
        await ctrl_agent.disconnect()

    asyncio.run(scenario())

    # DB assertions after asyncio.run (sync context).
    gate = AudioGate.objects.filter(station=station).first()
    # After lock_release the PTT must be cleared.
    assert gate is None or gate.ptt_active is False


@pytest.mark.django_db(transaction=True)
def test_control_keepalive_refreshes_ptt(control_agent_auth, audio_agent_auth):
    """ptt_keepalive while holding lock → gate.ptt_expires_at extended."""
    from apps.audio import gate as audio_gate
    from apps.audio.models import AudioGate
    from apps.control import lock as control_lock

    station = Station.objects.create(name="h3", status="online")
    holder = User.objects.create(
        username="holder_h3", membership_level=User.MembershipLevel.MEMBER
    )
    control_lock.acquire(station, holder)
    audio_gate.set_ptt(station, slot=0, module="fm")

    gate_before = AudioGate.objects.get(station=station)
    original_expires = gate_before.ptt_expires_at
    assert original_expires is not None, "set_ptt must set expires_at"

    # Capture expires_at inside the async run (before agent disconnect clears it).
    captured = {}

    async def scenario():
        from channels.db import database_sync_to_async

        from tests.test_control_consumer_relay import _agent_comm as ctrl_agent_comm

        ctrl_agent = ctrl_agent_comm(station.id)
        assert (await ctrl_agent.connect())[0] is True

        hc = WebsocketCommunicator(application, f"/ws/control/{station.id}/")
        hc.scope["user"] = holder
        assert (await hc.connect())[0] is True

        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # inventory
        await asyncio.wait_for(hc.receive_json_from(), timeout=2.0)  # lock

        # Send keepalive (holder already has lock).
        await hc.send_json_to({"type": "ptt_keepalive", "slot": 0, "module": "fm"})
        # Small delay for async processing.
        await asyncio.sleep(0.2)

        # Read expires_at BEFORE disconnect (agent disconnect clears the gate).
        def _get_expires():
            return (
                AudioGate.objects.filter(station=station)
                .values_list("ptt_expires_at", flat=True)
                .first()
            )

        captured["expires_at"] = await database_sync_to_async(_get_expires)()

        await hc.disconnect()
        await ctrl_agent.disconnect()

    asyncio.run(scenario())

    # Keepalive must have extended the expiry.
    assert captured.get("expires_at") is not None
    assert captured["expires_at"] >= original_expires


# ---------------------------------------------------------------------------
# 8. Security / validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_subscribe_malformed_stream_id_yields_error_no_db_row(audio_agent_auth):
    """Browser sends subscribe with malformed stream_id 'slot0/rx' (contains slash).

    Slash is not in [a-zA-Z\\d\\-_.] → must be rejected with an error frame,
    must NOT crash the consumer, and must leave NO AudioSubscription row.
    """
    from apps.audio.models import AudioSubscription

    station = Station.objects.create(name="sec1", status="online")
    user = User.objects.create(
        username="member_sec1", membership_level=User.MembershipLevel.MEMBER
    )

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True

        # Send subscribe with malformed stream_id (slash is forbidden).
        await browser.send_json_to(
            {"v": V, "type": "subscribe", "stream_ids": ["slot0/rx"]}
        )

        # Consumer must send back an error frame.
        err = await _drain_until(browser, "error")
        assert err["code"] == "unknown_stream"

        # Consumer must still be alive (send a valid subscribe and get response).
        await browser.send_json_to(
            {"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]}
        )
        # The valid subscribe triggers source_subscribe to the agent.
        sub = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert sub["type"] == "source_subscribe"
        assert sub["stream_id"] == "slot0.rx"

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())

    # No demand row for the malformed id.
    assert AudioSubscription.objects.filter(station=station, stream_id="slot0/rx").count() == 0


@pytest.mark.django_db(transaction=True)
def test_subscribe_before_agent_connect_demand_resend(audio_agent_auth):
    """Browser subscribes to slot0.rx BEFORE the agent connects.

    When the agent then connects and sends advertise, the _handle_advertise
    demand re-send path must issue source_subscribe for slot0.rx to the agent.
    (Exercises _handle_advertise's 'cnt > 0' re-send loop — m-4.)
    """
    station = Station.objects.create(name="m4", status="online")
    user = User.objects.create(
        username="member_m4", membership_level=User.MembershipLevel.MEMBER
    )
    advertise = _load_json("advertise.json")

    async def scenario():
        # Browser connects and subscribes FIRST — no agent yet.
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True

        await browser.send_json_to(
            {"v": V, "type": "subscribe", "stream_ids": ["slot0.rx"]}
        )
        # No agent yet → no source_subscribe can arrive anywhere; that is fine.
        # Give the subscribe DB write a moment.
        await asyncio.sleep(0.1)

        # Now the agent connects.
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True

        # Agent sends advertise → _handle_advertise detects demand (cnt > 0) and
        # re-sends source_subscribe for slot0.rx.
        await agent.send_json_to(advertise)

        # The agent must receive source_subscribe for slot0.rx.
        msg = await asyncio.wait_for(agent.receive_json_from(), timeout=2.0)
        assert msg["type"] == "source_subscribe"
        assert msg["stream_id"] == "slot0.rx"

        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())


@pytest.mark.django_db(transaction=True)
def test_agent_auth_reject_no_exception_during_teardown():
    """Full connect(fail-sig)→disconnect on the auth-reject path.

    Guards B-1: disconnect() must NOT raise AttributeError when station is None
    (the reject path never sets self.station) nor when it references
    self.browser_group (now assigned before the auth guard).

    Does NOT use the audio_agent_auth fixture so the real reject path runs.
    Instead we monkeypatch _verify_agent to return False via an async function.
    """
    import unittest.mock

    from apps.audio import consumers as audio_consumers

    station = Station.objects.create(name="m5", status="online")

    async def _reject_verify(self, station, params):
        return False

    async def scenario():
        # Patch _verify_agent to always return False (auth rejected).
        with unittest.mock.patch.object(
            audio_consumers.AgentAudioConsumer,
            "_verify_agent",
            new=_reject_verify,
        ):
            agent = WebsocketCommunicator(
                application,
                f"/ws/agent/audio/{station.id}/?signature=bad&timestamp=0",
            )
            # connect() should fail with code 4401.
            connected, code = await agent.connect()
            assert connected is False
            assert code == 4401
            # disconnect() must not raise — in particular no AttributeError on
            # self.browser_group / self.agent_group (B-1 fix).
            # WebsocketCommunicator.disconnect() is a no-op on an already-closed
            # socket but exercises the teardown path; errors surface as exceptions.
            await agent.disconnect()

    # asyncio.run propagates any exception from the coroutine including ones
    # raised inside the consumer's disconnect() (they bubble via the Channels
    # test layer).
    asyncio.run(scenario())
