"""Unit tests for apps/audio/orchestrator.py.

Task 9: orchestrator-level correlation WITHOUT a real agent.
The round-trip test uses a WebsocketCommunicator (real AgentAudioConsumer) as
the agent stand-in — same pattern as test_audio_diag_consumer.py — so the
group_send routing works exactly as in production.

No pytest-asyncio — asyncio.run() inside @pytest.mark.django_db(transaction=True).
"""

import asyncio
import json

import pytest
from channels.testing import WebsocketCommunicator

from apps.audio.orchestrator import (
    REF_STREAM_SETTLE_S,
    AgentNotConnected,
    DiagnosticTimeout,
    StationBusy,
    run_headless_diagnostic,
)
from apps.stations.models import Station
from config.asgi import application
from station_agent.audio import frame as audio_frame
from station_agent.audio.diagnostics import DIAG_STREAM_REF

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_SIGNAL = {"kind": "sine", "freq_hz": 1000, "level_dbfs": -20.0, "duration_ms": 500}

_SAMPLE_AGENT_REPORT = {
    "type": "diag_result",
    "anchor": "C",
    "reference": {},
    "taps": [
        {
            "point": "C",
            "rms_dbfs": -6.0,
            "peak_dbfs": -3.0,
            "silent": False,
            "computed": False,
        },
        {
            "point": "D",
            "rms_dbfs": -13.96,
            "peak_dbfs": -10.96,
            "silent": False,
            "computed": True,
        },
    ],
    "static_gains": {
        "sink_volume_linear": 0.4,
        "sink_volume_db": -7.96,
    },
}


def _agent_comm(station_id):
    """Ed25519 verification bypassed via audio_agent_auth fixture."""
    return WebsocketCommunicator(
        application, f"/ws/agent/audio/{station_id}/?signature=x&timestamp=0"
    )


# ---------------------------------------------------------------------------
# Test: full round-trip via real AgentAudioConsumer + in-memory channel layer
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_orchestrator_roundtrip(audio_agent_auth):
    """The orchestrator sends a diag_command to agent_group, the real
    AgentAudioConsumer routes the reply to the reply channel, and the
    orchestrator returns build_run_report(result) — which has stages + verdict."""
    station = Station.objects.create(name="orch-test", callsign="OE1ORC", status="online")

    async def scenario():
        # Connect a real AgentAudioConsumer to simulate the agent.
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True

        async def fake_agent_reply():
            """Drain the diag_command from the WS, send back a diag_result."""
            msg = await asyncio.wait_for(agent.receive_json_from(), timeout=3.0)
            assert msg["type"] == "diag_command"
            rid = msg["request_id"]
            report = dict(_SAMPLE_AGENT_REPORT)
            report["request_id"] = rid
            await agent.send_to(text_data=json.dumps(report))

        agent_task = asyncio.ensure_future(fake_agent_reply())
        result = await run_headless_diagnostic(station.id, "C", _SAMPLE_SIGNAL)
        await agent_task  # propagate assertion errors from fake_agent_reply

        await agent.disconnect()

        # build_run_report was called — result must have stages + verdict.
        assert "stages" in result
        assert "verdict" in result
        assert result["anchor"] == "C"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: timeout raises DiagnosticTimeout
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_orchestrator_timeout():
    """No agent replies → DiagnosticTimeout raised after timeout."""
    station = Station.objects.create(name="orch-timeout", callsign="OE1TMO", status="online")

    async def scenario():
        with pytest.raises(DiagnosticTimeout):
            await run_headless_diagnostic(station.id, "C", _SAMPLE_SIGNAL, timeout=0.3)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: offline station raises AgentNotConnected
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_orchestrator_offline_station():
    """Station status != 'online' → AgentNotConnected raised immediately."""
    station = Station.objects.create(name="orch-offline", callsign="OE1OFL", status="offline")

    async def scenario():
        with pytest.raises(AgentNotConnected):
            await run_headless_diagnostic(station.id, "C", _SAMPLE_SIGNAL)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: missing station raises AgentNotConnected
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_orchestrator_missing_station():
    """Non-existent station_id → AgentNotConnected raised immediately."""

    async def scenario():
        with pytest.raises(AgentNotConnected):
            # Use a pk that can't exist.
            await run_headless_diagnostic(999_999_999, "C", _SAMPLE_SIGNAL)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: U-anchor reference frames use DIAG_STREAM_REF
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_u_frames_use_diag_stream_ref(audio_agent_auth):
    """Every audio.ref_media frame streamed during a U diagnostic must carry
    stream_ref == DIAG_STREAM_REF (not the old OP_MIC_DIAG_REF stub of 0).

    Strategy: intercept channel-layer group_send calls to capture audio.ref_media
    payloads without involving the WebSocket consumer (so we don't accidentally
    cancel it via receive_output timeout).  A real agent communicator still handles
    the diag_command / diag_result round-trip.
    """
    from channels.layers import get_channel_layer

    station = Station.objects.create(name="orch-uref", callsign="OE1URF", status="online")

    async def scenario():
        layer = get_channel_layer()
        captured_ref_frames: list[bytes] = []
        _original_group_send = layer.group_send

        async def spy_group_send(group, message):
            if message.get("type") == "audio.ref_media":
                captured_ref_frames.append(message["data"])
            await _original_group_send(group, message)

        layer.group_send = spy_group_send

        try:
            agent = _agent_comm(station.id)
            connected, _ = await agent.connect()
            assert connected is True

            async def fake_agent_reply():
                """Drain the diag_command, wait past the settle window, then reply.

                A real accepted U run never sends anything during the settle window
                (the agent only emits a result after receiving ref frames).  Delay
                by slightly more than REF_STREAM_SETTLE_S so the orchestrator's
                bounded-receive times out → fixture frames are streamed → then the
                reply is consumed from the post-stream await as expected.
                """
                msg = await asyncio.wait_for(agent.receive_json_from(), timeout=3.0)
                assert msg["type"] == "diag_command"
                rid = msg["request_id"]
                # Wait past the settle window so the early-refusal check times out.
                await asyncio.sleep(REF_STREAM_SETTLE_S + 0.1)
                report = dict(_SAMPLE_AGENT_REPORT)
                report["anchor"] = "U"
                report["request_id"] = rid
                await agent.send_to(text_data=json.dumps(report))

            agent_task = asyncio.ensure_future(fake_agent_reply())
            await run_headless_diagnostic(station.id, "U", _SAMPLE_SIGNAL)
            await agent_task
            await agent.disconnect()
        finally:
            layer.group_send = _original_group_send

        # Every captured ref frame must parse with stream_ref == DIAG_STREAM_REF.
        assert len(captured_ref_frames) > 0, "No audio.ref_media frames were streamed"
        for data in captured_ref_frames:
            parsed = audio_frame.parse_frame(data)
            assert parsed.stream_ref == DIAG_STREAM_REF, (
                f"Expected stream_ref={DIAG_STREAM_REF:#06x}, got {parsed.stream_ref:#06x}"
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: busy reply raises StationBusy
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_busy_reply_raises_station_busy(audio_agent_auth):
    """Agent reply with busy=True → orchestrator raises StationBusy."""
    station = Station.objects.create(name="orch-busy", callsign="OE1BSY", status="online")

    async def scenario():
        agent = _agent_comm(station.id)
        connected, _ = await agent.connect()
        assert connected is True

        async def fake_busy_reply():
            """Drain the diag_command, send a busy report."""
            msg = await asyncio.wait_for(agent.receive_json_from(), timeout=3.0)
            assert msg["type"] == "diag_command"
            rid = msg["request_id"]
            # Reply with a busy envelope (anchor C has no binary ref frames).
            busy_report = {
                "type": "diag_result",
                "request_id": rid,
                "busy": True,
                "error": "refused: TX active",
            }
            await agent.send_to(text_data=json.dumps(busy_report))

        agent_task = asyncio.ensure_future(fake_busy_reply())
        with pytest.raises(StationBusy):
            await run_headless_diagnostic(station.id, "C", _SAMPLE_SIGNAL)
        await agent_task

        await agent.disconnect()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Test: U busy during settle → StationBusy + zero ref frames streamed
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_u_busy_during_settle_no_ref_frames(audio_agent_auth):
    """Anchor-U request where the agent replies busy during the settle window
    must raise StationBusy and stream ZERO audio.ref_media frames.

    This is the core regression guard for the cross-run ref-frame pollution bug:
    a refused U run must not inject its fixture frames into the channel group,
    because those frames carry the global DIAG_STREAM_REF and would be routed
    into the first (running) diagnostic's bridge.
    """
    from channels.layers import get_channel_layer

    station = Station.objects.create(name="orch-ubsy", callsign="OE1UBS", status="online")

    async def scenario():
        layer = get_channel_layer()
        ref_frame_count: list[int] = [0]
        _original_group_send = layer.group_send

        async def spy_group_send(group, message):
            if message.get("type") == "audio.ref_media":
                ref_frame_count[0] += 1
            await _original_group_send(group, message)

        layer.group_send = spy_group_send

        try:
            agent = _agent_comm(station.id)
            connected, _ = await agent.connect()
            assert connected is True

            async def fake_u_busy_reply():
                """Drain the diag_command, immediately send a busy report."""
                msg = await asyncio.wait_for(agent.receive_json_from(), timeout=3.0)
                assert msg["type"] == "diag_command"
                rid = msg["request_id"]
                busy_report = {
                    "type": "diag_result",
                    "request_id": rid,
                    "anchor": "U",
                    "busy": True,
                    "error": "refused: diagnostic already active",
                }
                await agent.send_to(text_data=json.dumps(busy_report))

            agent_task = asyncio.ensure_future(fake_u_busy_reply())
            with pytest.raises(StationBusy):
                await run_headless_diagnostic(station.id, "U", _SAMPLE_SIGNAL)
            await agent_task
            await agent.disconnect()
        finally:
            layer.group_send = _original_group_send

        # Regression guard: a refused U run must not stream ANY ref frames.
        assert ref_frame_count[0] == 0, (
            f"Expected 0 audio.ref_media frames for a refused U run, got {ref_frame_count[0]}"
        )

    asyncio.run(scenario())
