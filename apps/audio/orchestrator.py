"""Headless diagnostic orchestrator for TX audio-path diagnostics.

Drives one diagnostic run over the channel layer without a live browser
WebSocket.  The caller sends a diag_command to the agent group, optionally
streams a U-anchor reference, and awaits the correlated diag_result reply.

Note: the agent-side op.mic ref matching and live measfd read for anchor U is
validated on test-station 211.  CI covers the control/transport flow.
"""

from __future__ import annotations

import asyncio
import uuid

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from apps.audio.constants import agent_group
from apps.audio.diagnostics import build_run_report, iter_media_frames, load_reference_frames

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Numeric stream_ref used when injecting the U-anchor op.mic reference.
OP_MIC_DIAG_REF: int = 0

#: Number of times to loop the reference frame list for anchor-U injection.
REF_REPEAT: int = 3


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentNotConnected(Exception):  # noqa: N818
    """Raised when the target station is not online / agent not reachable."""


class DiagnosticTimeout(Exception):  # noqa: N818
    """Raised when the agent does not reply within the timeout window."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_headless_diagnostic(
    station_id,
    anchor: str,
    signal: dict,
    *,
    slot: int = 0,
    timeout: float = 15.0,
) -> dict:
    """Drive one diagnostic run and return the assembled run report.

    Parameters
    ----------
    station_id:
        Primary key of the target :class:`~apps.stations.models.Station`.
    anchor:
        Measurement anchor — ``"U"`` (upstream, op.mic path) or ``"C"``
        (capture tap, downstream path).
    signal:
        Signal descriptor forwarded verbatim to the agent.
    slot:
        Audio module slot index (default 0).
    timeout:
        Seconds to wait for the agent reply before raising
        :class:`DiagnosticTimeout`.

    Returns
    -------
    dict
        Assembled run report from :func:`~apps.audio.diagnostics.build_run_report`.

    Raises
    ------
    AgentNotConnected
        If the station does not exist or its status is not ``"online"``.
    DiagnosticTimeout
        If the agent does not reply within *timeout* seconds.
    """
    # 1. Presence check — fail fast if the station is not online.
    station = await _get_station(station_id)
    if station is None or station.status != "online":
        raise AgentNotConnected()

    # 2. Allocate a request id + reply channel.
    rid = uuid.uuid4().hex
    layer = get_channel_layer()
    reply = await layer.new_channel()

    # 3. Build and send the diag_command to the agent group.
    command = {
        "v": 1,
        "type": "diag_command",
        "request_id": rid,
        "anchor": anchor,
        "slot": slot,
        "signal": signal,
    }
    await layer.group_send(
        agent_group(station_id),
        {
            "type": "audio.diag_command",
            "request_id": rid,
            "reply_channel": reply,
            "command": command,
        },
    )

    # 4. For anchor U: stream the reference frames after sending the command.
    if anchor == "U":
        frames = load_reference_frames()
        for data in iter_media_frames(frames, stream_ref=OP_MIC_DIAG_REF, repeat=REF_REPEAT):
            await layer.group_send(
                agent_group(station_id),
                {"type": "audio.ref_media", "data": data},
            )

    # 5. Await the correlated reply.
    try:
        envelope = await asyncio.wait_for(layer.receive(reply), timeout)
    except TimeoutError:
        raise DiagnosticTimeout()

    # 6. Surface agent-side errors without calling build_run_report.
    agent_report = envelope["msg"]
    if agent_report.get("error"):
        return {"anchor": anchor, "error": agent_report["error"]}

    # 7. Assemble and return the full run report.
    return build_run_report(agent_report)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@database_sync_to_async
def _get_station(station_id):
    from apps.stations.models import Station

    try:
        return Station.objects.get(pk=station_id)
    except Station.DoesNotExist:
        return None
