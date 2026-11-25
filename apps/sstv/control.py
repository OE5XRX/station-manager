"""SSTV receive control on the server (SSTV spec §5.2, §5.3).

The lock holder starts and stops the station's ``sstv`` decoder; this module keeps the
station's ``ActiveSstvRx`` row in step with it, makes RX and the repeater scanner
exclusive ("last one started wins"), ends RX (and aborts TX) on every lock change, and
reports the RX state to the control page.

All functions are synchronous (consumers wrap them in ``database_sync_to_async``). Frames
to the agent and broadcasts to the browsers go out on commit.
"""

import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from apps.control import agent_presence, constants, scan
from apps.control.models import ControlLock, StationModule

from .models import ActiveSstvRx, SstvTransmission

logger = logging.getLogger(__name__)

DECODER = "sstv"
# Feature the agent advertises when it can receive AND send SSTV (station_agent.decoders).
FEATURE = "sstv"
MAX_RUNTIME_S = 1800
# A start the agent has not confirmed (no decoder_state running) keeps the lock busy only
# this long: an agent that ignores decoder_start must not hold the lock forever.
START_GRACE_S = 30.0
# Request ids of server-issued SSTV agent requests: their results go to apps.sstv, never
# to the browsers.
REQUEST_PREFIX = "sstv-"
RX_START_REQUEST = f"{REQUEST_PREFIX}rx-start"
RX_STOP_REQUEST = f"{REQUEST_PREFIX}rx-stop"
_DETAIL_MAX = 64
_LIVE_STATES = (ActiveSstvRx.State.STARTING, ActiveSstvRx.State.RUNNING, ActiveSstvRx.State.PAUSED)


def browser_group(station_pk):
    return f"control_{station_pk}"


def _frame(mtype, **fields):
    return {"v": constants.PROTOCOL_VERSION, "type": mtype, **fields}


def _group_send(group, event):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, event)
    except Exception:
        logger.exception("sstv: group send %s failed", event.get("type"))


def send_to_agent(station_pk, frame):
    """Send ``frame`` to the station's agent once the current transaction commits."""
    transaction.on_commit(
        lambda: _group_send(
            scan.agent_group(station_pk), {"type": "control.to_agent", "frame": frame}
        )
    )


def broadcast(station_pk, msg):
    """Push ``msg`` to every control page of the station once the transaction commits."""
    transaction.on_commit(
        lambda: _group_send(browser_group(station_pk), {"type": "control.sstv", "msg": msg})
    )


def _audit(station, user, message):
    from apps.stations.models import StationAuditLog

    StationAuditLog.log(station=station, event_type="control_command", message=message, user=user)


def _wire_slot(slot):
    return int(slot) if isinstance(slot, str) and slot.isdigit() else slot


def _clip(value):
    return str(value)[:_DETAIL_MAX] if value is not None else None


def is_holder(station, user):
    holder = (
        ControlLock.objects.filter(station=station, scope="station")
        .values_list("holder_id", flat=True)
        .first()
    )
    return user is not None and holder is not None and holder == user.pk


def supported(station):
    """The station's agent advertised SSTV (decoder plugin and TX session)."""
    return agent_presence.has_feature(station, FEATURE)


def state_frame(station, detail=None):
    """``sstv_state`` for the control page: the running RX session or ``idle``."""
    row = ActiveSstvRx.objects.filter(station=station, state__in=_LIVE_STATES).first()
    if row is None:
        return _frame("sstv_state", state="idle", detail=_clip(detail), slot=None, ends_at=None)
    ends_at = row.started_at + timedelta(seconds=MAX_RUNTIME_S)
    return _frame(
        "sstv_state",
        state=row.state,
        detail=_clip(detail),
        slot=row.slot,
        ends_at=ends_at.isoformat(),
    )


def live_state_frame(station):
    """``state_frame`` of a running RX session, ``None`` when idle."""
    if not ActiveSstvRx.objects.filter(station=station, state__in=_LIVE_STATES).exists():
        return None
    return state_frame(station)


def _rx_module(station, slot):
    return (
        StationModule.objects.filter(station=station, slot=str(slot), online=True)
        .order_by("module_id")
        .first()
    )


def _decoder_start(station, slot, max_runtime_s):
    send_to_agent(
        station.pk,
        _frame(
            "decoder_start",
            request_id=RX_START_REQUEST,
            decoder=DECODER,
            slot=_wire_slot(str(slot)),
            params={},
            max_runtime_s=max_runtime_s,
        ),
    )


def _decoder_stop(station, slot):
    send_to_agent(
        station.pk,
        _frame(
            "decoder_stop",
            request_id=RX_STOP_REQUEST,
            decoder=DECODER,
            slot=_wire_slot(str(slot)),
        ),
    )


def start_rx(station, user, slot):
    """Start SSTV receive on ``slot`` for the lock holder ``user``.

    Returns an error code (``not_locked``, ``unsupported``, ``invalid_slot``,
    ``sstv_busy``) or ``None``. Stops a running scan first; a running RX session (also on
    another slot) is replaced."""
    if not is_holder(station, user):
        return "not_locked"
    if not supported(station):
        return "unsupported"
    if not (isinstance(slot, str) or type(slot) is int) or not str(slot).isdigit():
        return "invalid_slot"
    if _rx_module(station, slot) is None:
        return "invalid_slot"
    slot = str(slot)
    with transaction.atomic():
        if SstvTransmission.objects.filter(
            station=station, state=SstvTransmission.State.SENDING
        ).exists():
            return "sstv_busy"
        previous = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        if previous is not None:
            if previous.state in _LIVE_STATES and str(previous.slot) != slot:
                _decoder_stop(station, str(previous.slot))
            previous.delete()
        scan.stop_active_scan(station, "sstv started")
        ActiveSstvRx.objects.create(
            station=station, slot=int(slot), user=user, state=ActiveSstvRx.State.STARTING
        )
        _audit(station, user, f"{user.username} started SSTV receive (slot {_clip(slot)})")
        _decoder_start(station, slot, MAX_RUNTIME_S)
        broadcast(station.pk, state_frame(station))
    return None


def stop_rx(station, reason, *, notify_agent=True):
    """End the station's RX session from the server side (operator, scan start, lock loss,
    agent gone). The row stays as ``stopping`` until the agent reports the decoder stopped,
    so the final (partial) picture is still attributed. True if a session was running."""
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update()
            .select_related("user")
            .filter(station=station, state__in=_LIVE_STATES)
            .first()
        )
        if row is None:
            return False
        was_paused = row.state == ActiveSstvRx.State.PAUSED
        row.state = ActiveSstvRx.State.STOPPING
        row.save(update_fields=["state"])
        _audit(station, row.user, f"SSTV receive stopped ({_clip(reason)})")
        if notify_agent and not was_paused:
            _decoder_stop(station, str(row.slot))
        broadcast(station.pk, state_frame(station, detail=reason))
    return True


def pause_rx(station):
    """TX pauses RX (spec §5.2): stop the decoder, keep the session. True if paused."""
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update()
            .filter(
                station=station,
                state__in=(ActiveSstvRx.State.STARTING, ActiveSstvRx.State.RUNNING),
            )
            .first()
        )
        if row is None:
            return False
        row.state = ActiveSstvRx.State.PAUSED
        row.save(update_fields=["state"])
        _decoder_stop(station, str(row.slot))
        broadcast(station.pk, state_frame(station, detail="sstv_tx"))
    return True


def resume_rx(station):
    """Restart a paused RX session for the rest of its runtime (ends it if none is left)."""
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update()
            .filter(station=station, state=ActiveSstvRx.State.PAUSED)
            .first()
        )
        if row is None:
            return False
        left = MAX_RUNTIME_S - (timezone.now() - row.started_at).total_seconds()
        if left < 1 or not is_holder(station, row.user):
            row.delete()
            broadcast(station.pk, state_frame(station, detail="max_runtime"))
            return False
        row.state = ActiveSstvRx.State.STARTING
        row.save(update_fields=["state"])
        _decoder_start(station, str(row.slot), int(left))
        broadcast(station.pk, state_frame(station))
    return True


def on_decoder_state(station, msg):
    """Agent ``decoder_state`` of the ``sstv`` decoder."""
    state, slot = msg.get("state"), msg.get("slot")
    if not (isinstance(slot, str) or type(slot) is int):
        return
    detail = msg.get("detail")
    with transaction.atomic():
        row = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        if row is None or str(row.slot) != str(slot):
            return
        if state == "running":
            if row.state == ActiveSstvRx.State.STARTING:
                _mark_running(station)
        elif state in ("stopped", "error"):
            if row.state == ActiveSstvRx.State.PAUSED:
                return  # our own pause stop (or a late one): resumed after the TX
            if row.state == ActiveSstvRx.State.STARTING and state == "stopped":
                return  # the stop of the instance this start replaces
            live = row.state != ActiveSstvRx.State.STOPPING
            row.delete()
            if live:
                _audit(station, None, f"SSTV receive ended ({_clip(detail or state)})")
                frame = state_frame(station, detail=detail or state)
                if state == "error":
                    frame["state"] = "error"
                broadcast(station.pk, frame)


def _mark_running(station):
    """The agent confirmed the decoder (``running`` state or an ok ``decoder_start``
    result — a start for a decoder already running on the slot only gets the result)."""
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update()
            .filter(station=station, state=ActiveSstvRx.State.STARTING)
            .first()
        )
        if row is None:
            return
        row.state = ActiveSstvRx.State.RUNNING
        row.save(update_fields=["state"])
        broadcast(station.pk, state_frame(station))


def on_rx_result(station, msg):
    """``result`` of our ``decoder_start``/``decoder_stop``. A refused start is reported by
    the agent as ``decoder_state error`` as well (handled there)."""
    if msg.get("request_id") == RX_START_REQUEST and msg.get("ok") is True:
        _mark_running(station)


def on_snapshot(station, decoders):
    """Agent ``decoder_snapshot``: forget sessions the agent no longer runs, stop SSTV
    decoders nobody started here (SSTV never runs on a station nobody holds)."""
    running = {
        str(d.get("slot"))
        for d in decoders
        if isinstance(d, dict) and d.get("decoder") == DECODER and str(d.get("slot")).isdigit()
    }
    with transaction.atomic():
        row = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        mine = str(row.slot) if row is not None and row.state in _LIVE_STATES else None
        if (
            row is not None
            and row.state
            in (
                ActiveSstvRx.State.STARTING,
                ActiveSstvRx.State.RUNNING,
            )
            and mine not in running
        ):
            row.state = ActiveSstvRx.State.STOPPING
            row.save(update_fields=["state"])
            broadcast(station.pk, state_frame(station, detail="agent_restarted"))
        for slot in sorted(running - {mine}, key=int)[:8]:
            _decoder_stop(station, slot)


def agent_gone(station):
    """The agent link died: it stopped SSTV itself; never resend anything on reconnect."""
    stop_rx(station, "agent_disconnect", notify_agent=False)
    from . import tx

    tx.abort_tx(station, "agent_disconnect", notify_agent=False)


def counts_as_activity(station):
    """A running RX session or a transmission on the air keeps the lock from idling out."""
    fresh = timezone.now() - timedelta(seconds=START_GRACE_S)
    rx = (
        ActiveSstvRx.objects.filter(station=station)
        .filter(state__in=(ActiveSstvRx.State.RUNNING, ActiveSstvRx.State.PAUSED))
        .exists()
        or ActiveSstvRx.objects.filter(
            station=station, state=ActiveSstvRx.State.STARTING, started_at__gte=fresh
        ).exists()
    )
    return (
        rx
        or SstvTransmission.objects.filter(
            station=station, state=SstvTransmission.State.SENDING
        ).exists()
    )


def _end_all(station, reason, notify_agent=True):
    from . import tx

    stop_rx(station, reason, notify_agent=notify_agent)
    tx.abort_tx(station, reason, notify_agent=notify_agent)


def _end_unless_user(station, reason, user):
    """Preempting oneself keeps one's own session (like the scanner)."""
    from . import tx

    row = ActiveSstvRx.objects.filter(station=station, state__in=_LIVE_STATES).first()
    if row is None or user is None or row.user_id != user.pk:
        stop_rx(station, reason)
    tx.abort_tx(station, reason, unless_user=user)


async def on_lock_released(station, reason):
    """Lock hook: every release ends RX and aborts TX (``agent_disconnect``: the agent did
    it already, no frames)."""
    await database_sync_to_async(_end_all)(
        station, f"lock {reason}", notify_agent=reason != "agent_disconnect"
    )


async def on_lock_acquired(station, holder, reason):
    """Lock hook: the previous holder lost the lock (transfer/preempt) -> their SSTV ends."""
    if reason == "transferred":
        await database_sync_to_async(_end_all)(station, "lock transferred")
    elif reason == "preempted":
        await database_sync_to_async(_end_unless_user)(station, "lock preempted", holder)
