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
    AgentNotConnected,
    DiagnosticTimeout,
    run_headless_diagnostic,
)
from apps.stations.models import Station
from config.asgi import application

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
