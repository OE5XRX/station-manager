"""Headless diagnostic orchestrator for TX audio-path diagnostics.

Drives one diagnostic run over the channel layer without a live browser
WebSocket.  The caller sends a diag_command to the agent group, optionally
streams a U-anchor reference, and awaits the correlated diag_result reply.

Both anchor U (upstream, op.mic path) and anchor C (capture tap, downstream)
are supported.  Anchor U streams a reference signal to the agent group using
the dedicated :data:`~station_agent.audio.diagnostics.DIAG_STREAM_REF` sentinel
so the agent routes those frames only to the active diagnostic bridge.
"""

from __future__ import annotations

import asyncio
import uuid

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from apps.audio.constants import agent_group
from apps.audio.diagnostics import build_run_report, iter_media_frames, load_reference_frames
from station_agent.audio.diagnostics import DIAG_STREAM_REF

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of times to loop the reference frame list for anchor-U injection.
REF_REPEAT: int = 3

#: Seconds to wait after sending the diag_command before streaming reference frames.
#: The agent's MeasuredTxBridge.start() only returns from Popen; gst's udpsrc is not
#: yet bound at that point, so the earliest reference datagrams would be dropped (UDP
#: to an unbound port). This short settle lets the agent spawn gst and bind the udpsrc
#: port before frames arrive.  REF_REPEAT loops provide additional margin against any
#: residual startup jitter.  Keep well under the run timeout (~15 s).
REF_STREAM_SETTLE_S: float = 0.5


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentNotConnected(Exception):  # noqa: N818
    """Raised when the target station is not online / agent not reachable."""


class DiagnosticTimeout(Exception):  # noqa: N818
    """Raised when the agent does not reply within the timeout window."""


class StationBusy(Exception):  # noqa: N818
    """Raised when the agent refuses a diagnostic because the station is busy.

    The station is considered busy when a TX is active or another diagnostic is
    already in flight.  The REST layer maps this to HTTP 409 Conflict.
    """


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
        :class:`DiagnosticTimeout`.  For anchor U the agent reads ~3.5 s of
        PCM (settle + window + 3 s read timeout in ``finish_u_diagnostic``),
        so this value must be ≥ ~4 s or a U run will time out here before the
        agent can reply.  The default 15.0 s is well above that threshold.

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
        # Settle: give the agent time to spawn gst and bind the udpsrc port before the
        # first datagram arrives (see REF_STREAM_SETTLE_S for rationale).
        await asyncio.sleep(REF_STREAM_SETTLE_S)
        frames = load_reference_frames()
        for data in iter_media_frames(frames, stream_ref=DIAG_STREAM_REF, repeat=REF_REPEAT):
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
    if agent_report.get("busy"):
        raise StationBusy()
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
