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
import contextlib
import uuid

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from apps.audio.constants import agent_group
from apps.audio.diagnostics import build_run_report, iter_media_frames, load_reference_frames

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

#: Inter-frame sleep when streaming U-anchor reference frames.
#: RedisChannelLayer's default per-channel capacity is 100; streaming 3×81 = 243 frames
#: back-to-back overflows it and drops messages.  A 5 ms pause after each group_send
#: keeps the in-flight queue well under that limit (the consumer drains each frame to the
#: agent WS between sends) and loosely mirrors a real ~20 ms op.mic uplink cadence.
#: Total added latency ≈ 243 × 5 ms ≈ 1.2 s, well within the run timeout.
REF_FRAME_INTERVAL_S: float = 0.005


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
# Helpers
# ---------------------------------------------------------------------------


def diag_ref_for_request(rid: str) -> int:
    """Derive a per-run stream_ref in the reserved high band (≥ 0x8000).

    Takes the first 4 hex characters of *rid* (a UUID4 hex string) and maps
    them into ``[0x8000, 0xFFFF]`` so the ref can never collide with the
    small ascending slot/mic refs used by production streams.

    Parameters
    ----------
    rid:
        A request id — typically ``uuid.uuid4().hex``.

    Returns
    -------
    int
        A value in the range ``[0x8000, 0xFFFF]``.
    """
    return 0x8000 | (int(rid[:4], 16) & 0x7FFF)


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
    StationBusy
        If the agent refuses the run because the station is busy (another
        diagnostic or TX is active).  For anchor U this is detected during the
        settle window — before any fixture frames are streamed — so a refused run
        never injects frames into the channel group.
    """
    # 1. Presence check — fail fast if the station is not online.
    station = await _get_station(station_id)
    if station is None or station.status != "online":
        raise AgentNotConnected()

    # 2. Allocate a request id + reply channel.
    rid = uuid.uuid4().hex
    layer = get_channel_layer()
    reply = await layer.new_channel()

    # Per-run stream_ref in the reserved high band (≥ 0x8000) so frames can only
    # reach the run they belong to.  Real slot/mic refs are small ascending values
    # and can never collide with this range.
    diag_ref = diag_ref_for_request(rid)

    # 3. Build and send the diag_command to the agent group.
    command = {
        "v": 1,
        "type": "diag_command",
        "request_id": rid,
        "anchor": anchor,
        "slot": slot,
        "signal": signal,
        "diag_ref": diag_ref,
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

    # 4. For anchor U: use the settle window as an early-refusal check, then stream frames
    #    concurrently with awaiting the reply so streaming stops as soon as the result arrives.
    if anchor == "U":
        # Dual-purpose settle: wait up to REF_STREAM_SETTLE_S for an early reply on the
        # reply channel.
        #
        # - Accepted U run: the agent's _start_u_diagnostic returns None (it only emits a
        #   result after receiving frames), so nothing arrives during the settle → receive
        #   times out → fall through and start streaming concurrently with the reply wait.
        #   The settle window also lets the agent spawn gst and bind the udpsrc port before
        #   the first datagram arrives (see REF_STREAM_SETTLE_S for rationale).
        #
        # - Refused run (busy/error): the agent replies immediately (no I/O) → arrives
        #   within the settle window → handled here and returned WITHOUT streaming any
        #   frames.  This prevents fixture frames from reaching the channel group when
        #   refused; those frames carry the per-run diag_ref and would be wasted, and in
        #   the (improbable) event of a concurrent run with the same ref they could pollute
        #   its bridge.
        try:
            early_envelope = await asyncio.wait_for(layer.receive(reply), REF_STREAM_SETTLE_S)
        except TimeoutError:
            pass  # No early reply — agent accepted the run; proceed to concurrent stream+wait.
        else:
            # Early reply received — handle it and return without streaming.
            early_report = early_envelope["msg"]
            if early_report.get("busy"):
                raise StationBusy()
            if early_report.get("error"):
                return {"anchor": anchor, "error": early_report["error"]}
            # Unexpected early full report (shouldn't happen in practice, but handle cleanly).
            return build_run_report(early_report)

        # Agent accepted the run.  Stream the reference concurrently with the reply wait so
        # streaming stops as soon as the diag_result arrives — no wasted tail frames beyond
        # the agent's ~500 ms measurement window.
        # diag_ref is a 15-bit reserved-band value (≥ 0x8000); the concurrent-stop below
        # bounds any cross-run exposure window to the measurement duration.
        async def _stream_reference() -> None:
            frames = load_reference_frames()
            for data in iter_media_frames(frames, stream_ref=diag_ref, repeat=REF_REPEAT):
                await layer.group_send(
                    agent_group(station_id),
                    {"type": "audio.ref_media", "data": data},
                )
                await asyncio.sleep(REF_FRAME_INTERVAL_S)

        stream_task = asyncio.create_task(_stream_reference())
        try:
            envelope = await asyncio.wait_for(layer.receive(reply), timeout)
        except TimeoutError:
            raise DiagnosticTimeout()
        finally:
            stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task

        # Surface agent-side errors without calling build_run_report.
        agent_report = envelope["msg"]
        if agent_report.get("busy"):
            raise StationBusy()
        if agent_report.get("error"):
            return {"anchor": anchor, "error": agent_report["error"]}
        return build_run_report(agent_report)

    # 5. Await the reply (anchor C only — anchor U returns inside the if block above).
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
