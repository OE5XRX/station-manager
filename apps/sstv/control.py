"""SSTV receive control on the server (SSTV spec §5.2, §5.3).

The lock holder starts and stops the station's ``sstv`` decoder; this module keeps the
station's ``ActiveSstvRx`` row in step with it, makes RX and the repeater scanner
exclusive ("last one started wins"), ends RX (and aborts TX) on every lock change, and
reports the RX state to the control page.

All functions are synchronous (consumers wrap them in ``database_sync_to_async``). Frames
to the agent and broadcasts to the browsers go out on commit.
"""

import logging
import re
import uuid
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
# Upper bounds for "busy": RX ends after MAX_RUNTIME_S (the agent stops it; resumes get the
# rest), a transmission after download + the longest mode + the agent's hard stop.
RX_MAX_S = MAX_RUNTIME_S + 120
TX_MAX_S = 600
# Request ids of server-issued SSTV agent requests: their results go to apps.sstv, never
# to the browsers.
REQUEST_PREFIX = "sstv-"
RX_START_PREFIX = f"{REQUEST_PREFIX}rx-start-"
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


_SLOT_RE = re.compile(r"0|[1-9][0-9]{0,3}")


def slot_key(slot):
    """Canonical slot string (``"1"``) of an agent/browser slot, ``None`` if it is not a
    plain ASCII slot number (no ``"01"``, ``"²"``, bools)."""
    if type(slot) is int:
        slot = str(slot)
    return slot if isinstance(slot, str) and _SLOT_RE.fullmatch(slot) else None


def _wire_slot(slot):
    key = slot_key(slot)
    return int(key) if key is not None else slot


def _clip(value):
    return str(value)[:_DETAIL_MAX] if value is not None else None


def is_holder(station, user, *, lock_row=False):
    """``user`` holds the station's lock. ``lock_row``: row-lock the ControlLock (call inside
    the caller's transaction), so a concurrent release cannot slip between check and write."""
    rows = ControlLock.objects.filter(station=station, scope="station")
    if lock_row:
        rows = rows.select_for_update()
    holder = rows.values_list("holder_id", flat=True).first()
    return user is not None and holder is not None and holder == user.pk


_AUDIT_CODE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ =.-]{0,63}")


def audit_detail(value):
    """Agent-reported detail as safe audit text: only its leading code (``player_failed
    rc=1``, ``reader`` of ``reader: <exception>``), never free text that may carry
    addresses. Anything else becomes ``error``."""
    from apps.audit.scrub import scrub_message

    head = str(value).split(":", 1)[0].strip()[:64]
    if not _AUDIT_CODE_RE.fullmatch(head) or scrub_message(head) != head:
        return "error"
    return head


def sending_transmissions(station):
    """Transmissions on the air. Bounded: a ``sending`` row whose end frame was lost does not
    keep the station busy (or the lock alive) longer than any transmission can last."""
    fresh = timezone.now() - timedelta(seconds=TX_MAX_S)
    return SstvTransmission.objects.filter(
        station=station, state=SstvTransmission.State.SENDING, updated_at__gte=fresh
    )


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


def _new_start_request():
    return f"{RX_START_PREFIX}{uuid.uuid4().hex[:16]}"


def _decoder_start(station, slot, max_runtime_s, request_id):
    send_to_agent(
        station.pk,
        _frame(
            "decoder_start",
            request_id=request_id,
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
    slot = slot_key(slot)
    if slot is None or _rx_module(station, slot) is None:
        return "invalid_slot"
    with transaction.atomic():
        if not is_holder(station, user, lock_row=True):
            return "not_locked"
        if sending_transmissions(station).exists():
            return "sstv_busy"
        previous = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        if previous is not None:
            if previous.state in (ActiveSstvRx.State.STARTING, ActiveSstvRx.State.RUNNING):
                # Also on the same slot: the agent keeps a running instance (and its
                # max-runtime timer) on a repeated start, so restart it for a fresh window.
                _decoder_stop(station, str(previous.slot))
            previous.delete()
        scan.stop_active_scan(station, "sstv started")
        request_id = _new_start_request()
        ActiveSstvRx.objects.create(
            station=station,
            slot=int(slot),
            user=user,
            state=ActiveSstvRx.State.STARTING,
            start_request=request_id,
        )
        _audit(station, user, f"{user.username} started SSTV receive (slot {_clip(slot)})")
        _decoder_start(station, slot, MAX_RUNTIME_S, request_id)
        broadcast(station.pk, state_frame(station))
    return None


def stop_rx(station, reason, *, notify_agent=True):
    """End the station's RX session from the server side (operator, scan start, lock loss,
    agent gone). The row stays as ``stopping`` until the agent reports the decoder stopped,
    so the final (partial) picture is still attributed. True if a session was running."""
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update(of=("self",))
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
        row.start_request = _new_start_request()
        row.save(update_fields=["state", "start_request"])
        _decoder_start(station, str(row.slot), int(left), row.start_request)
        broadcast(station.pk, state_frame(station))
    return True


def on_decoder_state(station, msg):
    """Agent ``decoder_state`` of the ``sstv`` decoder."""
    state, slot = msg.get("state"), slot_key(msg.get("slot"))
    if slot is None:
        return
    detail = msg.get("detail")
    with transaction.atomic():
        row = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        if row is None or str(row.slot) != slot or row.state == ActiveSstvRx.State.STOPPING:
            if state == "running":
                # A decoder nobody here owns (e.g. a start that was replaced or refused
                # meanwhile, or one confirmed after its session was given up): SSTV never
                # runs on its own. A repeated stop is harmless.
                _decoder_stop(station, slot)
            if row is None or str(row.slot) != slot:
                return
        if row.state == ActiveSstvRx.State.STARTING:
            # States carry no request id: while a (re)start is pending they may belong to
            # the instance it replaces (its running/stopped/error). Only the result with
            # this start's request id confirms or ends it (``on_rx_result``).
            return
        if state in ("stopped", "error"):
            if row.state == ActiveSstvRx.State.PAUSED:
                return  # our own pause stop (or a late one): resumed after the TX
            live = row.state != ActiveSstvRx.State.STOPPING
            row.delete()
            if live:
                _audit(station, None, f"SSTV receive ended ({audit_detail(detail or state)})")
                frame = state_frame(station, detail=detail or state)
                if state == "error":
                    frame["state"] = "error"
                broadcast(station.pk, frame)


def _mark_running(station):
    """The agent confirmed this start (ok ``decoder_start`` result with its request id;
    the host always sends it, also for a decoder already running on the slot)."""
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
    """``result`` of our ``decoder_start``/``decoder_stop``. A refused start ends the
    session (usually a ``decoder_state error`` follows too; some refusals send only this)."""
    rid = msg.get("request_id")
    if not isinstance(rid, str) or not rid.startswith(RX_START_PREFIX):
        return
    with transaction.atomic():
        row = (
            ActiveSstvRx.objects.select_for_update()
            .filter(station=station, state=ActiveSstvRx.State.STARTING, start_request=rid)
            .first()
        )
        if row is None:
            return  # a result of an earlier (replaced/resumed) start
        if msg.get("ok") is True:
            _mark_running(station)
            return
        error = msg.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        row.delete()
        _audit(station, None, f"SSTV receive refused ({audit_detail(code or 'refused')})")
        frame = state_frame(station, detail=code or "refused")
        frame["state"] = "error"
        broadcast(station.pk, frame)


def on_snapshot(station, decoders):
    """Agent ``decoder_snapshot``: forget sessions the agent no longer runs, stop SSTV
    decoders nobody started here (SSTV never runs on a station nobody holds)."""
    running = {
        slot_key(d.get("slot"))
        for d in decoders
        if isinstance(d, dict) and d.get("decoder") == DECODER
    } - {None}
    with transaction.atomic():
        row = ActiveSstvRx.objects.select_for_update().filter(station=station).first()
        mine = str(row.slot) if row is not None and row.state in _LIVE_STATES else None
        # A fresh start may have been sent on this connection before the snapshot was
        # taken (the snapshot follows the inventory as a separate task): its own result
        # confirms or ends it. Only a start older than the grace is stale.
        stale_start = (
            row is not None
            and row.state == ActiveSstvRx.State.STARTING
            and (row.started_at < timezone.now() - timedelta(seconds=START_GRACE_S))
        )
        if (
            row is not None
            and (row.state == ActiveSstvRx.State.RUNNING or stale_start)
            and mine not in running
        ):
            row.state = ActiveSstvRx.State.STOPPING
            row.save(update_fields=["state"])
            broadcast(station.pk, state_frame(station, detail="agent_restarted"))
        for slot in sorted(running - {mine}, key=int)[:8]:
            _decoder_stop(station, slot)


def _isolated(fn, *args, **kwargs):
    """Run one teardown step; a failure is logged and never skips the next one (aborting a
    transmission must happen even if ending RX failed)."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("sstv: %s failed", fn.__name__)


def agent_gone(station):
    """The agent link died: it stopped SSTV itself; never resend anything on reconnect."""
    from . import tx

    _isolated(stop_rx, station, "agent_disconnect", notify_agent=False)
    _isolated(tx.abort_tx, station, "agent_disconnect", notify_agent=False)


def counts_as_activity(station):
    """A running RX session or a transmission on the air keeps the lock from idling out."""
    now = timezone.now()
    fresh = now - timedelta(seconds=START_GRACE_S)
    rx = (
        ActiveSstvRx.objects.filter(station=station)
        .filter(
            state__in=(ActiveSstvRx.State.RUNNING, ActiveSstvRx.State.PAUSED),
            started_at__gte=now - timedelta(seconds=RX_MAX_S),
        )
        .exists()
        or ActiveSstvRx.objects.filter(
            station=station, state=ActiveSstvRx.State.STARTING, started_at__gte=fresh
        ).exists()
    )
    return rx or sending_transmissions(station).exists()


def _end_all(station, reason, notify_agent=True):
    from . import tx

    _isolated(stop_rx, station, reason, notify_agent=notify_agent)
    _isolated(tx.abort_tx, station, reason, notify_agent=notify_agent)


def _end_unless_user(station, reason, user):
    """Preempting oneself keeps one's own session (like the scanner)."""
    from . import tx

    try:
        row = ActiveSstvRx.objects.filter(station=station, state__in=_LIVE_STATES).first()
        if row is None or user is None or row.user_id != user.pk:
            stop_rx(station, reason)
    except Exception:
        logger.exception("sstv: stopping RX on preempt failed")
    _isolated(tx.abort_tx, station, reason, unless_user=user)


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
