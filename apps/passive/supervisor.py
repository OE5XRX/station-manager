"""Per-agent-connection supervisor of the passive APRS service.

One instance lives in each ``AgentControlConsumer`` and is ticked from its 5 s sweep. It
tunes the radio with system-issued ``command`` frames, starts/stops the decoder with
``decoder_*`` frames, hands the radio back when somebody takes the control lock, and
reconciles with what the agent reports after a (re)connect.

Concurrency rules (the consumer dispatches frames one at a time):

* Everything that awaits an agent ``result`` runs in the sweep task or in a task spawned by
  :meth:`request_preempt` — never inline in ``receive`` / a group handler, because the
  ``result`` frame arrives through that very dispatch loop.
* ``_lock`` serialises start sequence, preempt and disconnect handling. A preempt cancels
  the pending futures first, so a half-way start unwinds at once and releases the lock.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone

from apps.control.models import StationModule
from apps.stations.models import Station, StationAuditLog

from . import profiles
from .eligibility import DECODERS_FEATURE, ineligible_reason
from .models import HandoverProfile, PassiveServiceConfig, PassiveServiceRun
from .selection import is_slot, pick_slot

logger = logging.getLogger(__name__)

SERVICE = "aprs"
REQUEST_PREFIX = "passive-"
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 900
# A run that never reports ``running`` (and has no start sequence in flight) is failed.
START_TIMEOUT_S = 60
# Without a decoder_snapshot this long after the inventory, reconcile as if it were empty.
SNAPSHOT_WAIT_S = 30
MAX_ORPHAN_SLOTS = 8
# ``note_user_set`` key meaning "a user took over the whole radio" (scan start).
WILDCARD_TOUCH = ("*", "*", "*")
_FREQUENCY_CAPS = ("frequency", "rx_frequency", "tx_frequency")

State = PassiveServiceRun.State

# Ineligible reasons that end an open run, with the hand-over they imply.
_PREEMPT_REASONS = {
    "disabled": ("disabled", False),
    "not_opted_in": ("not_opted_in", False),
    "updating": ("updating", False),
    "draining": ("draining", False),
    "no_module": ("no_module", False),
    "lock_held": ("acquired", True),
}


# Idle reasons that mean "not configured / not reachable": never worth a browser frame.
_QUIET_REASONS = frozenset({"disabled", "not_opted_in", "offline"})


class _AbortedError(Exception):
    """The start sequence was cancelled (preempt / disconnect)."""


class _CommandFailedError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _backoff_seconds(failures: int) -> int:
    return min(BACKOFF_BASE_S * 2 ** max(failures - 1, 0), BACKOFF_MAX_S)


class PassiveSupervisor:
    def __init__(
        self,
        station_id,
        send_to_agent,
        broadcast,
        *,
        now=timezone.now,
        command_timeout: float | None = None,
        wire_slot=None,
        is_current=None,
    ):
        self.station_id = station_id
        self._send = send_to_agent
        self._broadcast = broadcast
        self._now = now
        self._timeout = (
            command_timeout
            if command_timeout is not None
            else settings.CONTROL_COMMAND_TIMEOUT_SECONDS
        )
        self._wire_slot = wire_slot or (lambda slot: int(slot) if is_slot(slot) else slot)
        self._is_current = is_current  # async () -> bool; False = a newer agent connection won
        self._lock = asyncio.Lock()
        self._futures: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._features: set[str] | None = None  # None until the first inventory
        self._unsupported = False
        self._snapshot: list[dict] | None = None
        self._reconciled = False
        self._abort = False
        self._starting = False
        self._decoder_start_sent = False
        self._failures = 0
        self._retry_at = None
        self._protected: set[tuple[str, str, str]] = set()
        self._published = None
        self._gone = False  # sticky: the agent connection is closed
        self._features_at = None
        self._seeded = False
        self._dying: set[int] = set()
        self._user_touched: set[tuple[str, str, str]] = set()

    # -- sync accessors -------------------------------------------------------

    def set_agent_features(self, features) -> None:
        self._features = (
            {f for f in features if isinstance(f, str)} if isinstance(features, list) else set()
        )
        self._unsupported = False
        self._features_at = self._now()

    def note_user_set(self, key: tuple[str, str, str]) -> None:
        """A user wrote ``(slot, module, capability)``: a hand-over must not undo it.
        ``frequency``/``rx_frequency``/``tx_frequency`` are one knob for the hand-over (the
        combined ``frequency`` sets RX and TX), so a touch of one covers all three."""
        slot, module, cap = key
        if cap in _FREQUENCY_CAPS:
            self._user_touched.update((slot, module, c) for c in _FREQUENCY_CAPS)
        else:
            self._user_touched.add(key)

    def protected_caps(self) -> set[tuple[str, str, str]]:
        """``(slot, module, capability)`` the active passive profile owns: drift re-apply
        must not write these while a run is open."""
        return set(self._protected)

    # -- frames from the agent (never block: no awaiting of results) -----------

    async def on_result(self, msg: dict) -> None:
        fut = self._futures.pop(msg.get("request_id"), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def prepare(self) -> None:
        """Seed the protected caps from an open run, before the first drift re-apply of a
        (re)connection can overwrite the profile of a decoder that survived the outage."""
        if self._seeded:
            return
        self._seeded = True
        run = await self._open_run()
        if run is not None:
            self._protected = await database_sync_to_async(self._protected_for)(run)

    async def on_inventory(self) -> None:
        await self._maybe_reconcile()

    async def on_snapshot(self, msg: dict) -> None:
        decoders = msg.get("decoders")
        self._snapshot = (
            [d for d in decoders if isinstance(d, dict)] if isinstance(decoders, list) else []
        )
        await self._maybe_reconcile()

    async def on_decoder_state(self, msg: dict) -> None:
        if msg.get("decoder") != SERVICE:
            return
        state, detail = msg.get("state"), msg.get("detail")
        run = await self._open_run()
        if run is None or str(msg.get("slot")) != run.slot:
            return
        if state == "running":
            if run.state in (State.STARTING, State.AGENT_OFFLINE):
                await self._set_state(run, State.RUNNING)
                self._failures = 0
                self._retry_at = None
                await self._publish(run_state=State.RUNNING, slot=run.slot)
        elif state == "error" and run.state != State.STOPPING:
            self._spawn_died(run, str(detail or "decoder error"))
        elif state == "stopped" and run.state not in (State.STOPPING,):
            await self._close(run, State.STOPPED, "decoder_stopped")
            self._reset_run_memory()
            await self._publish()

    # -- scheduler -------------------------------------------------------------

    async def _current(self) -> bool:
        return self._is_current is None or await self._is_current()

    async def tick(self) -> None:
        if self._gone or not await self._current():
            return
        if self._features is not None and DECODERS_FEATURE in self._features:
            if not self._reconciled:
                waited = (self._now() - self._features_at).total_seconds()
                if waited < SNAPSHOT_WAIT_S:
                    return  # the agent's decoder_snapshot is on its way: no start before it
                self._snapshot = [] if self._snapshot is None else self._snapshot
                await self._maybe_reconcile()
        run = await self._open_run()
        if run is None:
            await self._tick_idle()
        else:
            await self._tick_open(run)

    async def _tick_idle(self) -> None:
        if self._retry_at is not None and self._now() < self._retry_at:
            return
        reason = await self._reason()
        if reason is not None:
            await self._publish(reason=reason)
            return
        await self._start()

    async def _tick_open(self, run) -> None:
        if run.state == State.AGENT_OFFLINE:
            # The decoder may well still run: wait for the snapshot of this connection.
            if self._features is not None and DECODERS_FEATURE not in self._features:
                await self._close(run, State.STOPPED, "agent_restarted")
                self._reset_run_memory()
                await self._publish()
            return
        if run.state == State.STOPPING:
            return
        reason = await self._reason(ignore_idle=True)
        decision = _PREEMPT_REASONS.get(reason)
        if decision is not None:
            end_reason, handover = decision
            await self.preempt(end_reason, handover=handover)
            return
        if (
            run.state == State.STARTING
            and not self._starting
            and (self._now() - run.started_at).total_seconds() > START_TIMEOUT_S
        ):
            self._spawn_died(run, "start_timeout")

    # -- start sequence --------------------------------------------------------

    async def _start(self) -> None:
        async with self._lock:
            self._abort = False
            self._starting = True
            self._decoder_start_sent = False
            try:
                await self._start_locked()
            except _AbortedError:
                pass
            finally:
                self._starting = False

    async def _start_locked(self) -> None:
        plan = await database_sync_to_async(self._create_run)()
        if plan is None:
            return
        run, module, values = plan
        self._protected = {
            (module.slot, module.module_id, cap) for cap in profiles.profile_caps(values, module)
        }
        self._user_touched.clear()
        await self._publish(run_state=State.STARTING, slot=run.slot)
        try:
            self._check_abort()
            # A grant between the eligibility check and the run row escaped the hook.
            if await self._lock_taken(handover=False):
                return
            for cap, value in profiles.profile_commands(values, module):
                await self._command(run.slot, module.module_id, cap, value)
                self._check_abort()
                if await self._lock_taken(handover=True):
                    return  # never start the decoder on a radio a user holds
            self._decoder_start_sent = True
            await self._request(
                {
                    "v": 1,
                    "type": "decoder_start",
                    "decoder": SERVICE,
                    "slot": int(run.slot),
                    "params": {},
                }
            )
        except _CommandFailedError as exc:
            await self._fail_start(run, exc.code)
            return
        await self._audit(f"system:passive_services started {SERVICE} on slot {run.slot}")

    async def _lock_taken(self, *, handover: bool) -> bool:
        """True (after ending the run as preempted) when a user holds the lock now."""
        free = await database_sync_to_async(self._lock_is_free)()
        self._check_abort()
        if free:
            return False
        # The start sequence ends here: the lock hook's own preempt (same grant) must not
        # cancel the hand-over commands that follow.
        self._starting = False
        await self._preempt_locked("acquired", handover=handover)
        return True

    def _check_abort(self) -> None:
        if self._abort:
            raise _AbortedError

    async def _fail_start(self, run, code: str) -> None:
        if code == "unsupported":
            self._unsupported = True
        if code not in ("timeout", "unsupported"):
            await self._apply_handover(run)  # the radio may be half-way on the APRS profile
        await self._close(run, State.ERROR, "start_failed", last_error=code)
        self._reset_run_memory()
        self._bump_backoff()
        await self._publish(reason=code)

    def _spawn_died(self, run, detail: str) -> None:
        """Single-flight per run: repeated error frames / ticks must not pile up tasks."""
        if run.pk in self._dying:
            return
        self._dying.add(run.pk)
        self._spawn(self._decoder_died(run, detail))

    async def _decoder_died(self, run, detail: str) -> None:
        async with self._lock:
            self._dying.discard(run.pk)
            current = await self._open_run()
            if current is None or current.pk != run.pk:
                return
            # STOPPING first: the agent announces ``stopped`` before it acks decoder_stop,
            # and that frame must not close the run as a plain "decoder_stopped".
            await self._set_state(current, State.STOPPING)
            try:
                await self._request(
                    {"v": 1, "type": "decoder_stop", "decoder": SERVICE, "slot": int(run.slot)}
                )
            except (_CommandFailedError, _AbortedError):
                logger.info(
                    "passive: stop after decoder error failed (station %s)", self.station_id
                )
            finally:
                # Also on cancellation / DB errors: never leave the run in STOPPING.
                await self._close(current, State.ERROR, "decoder_error", last_error=detail)
                self._reset_run_memory()
                self._bump_backoff()
            await self._publish(reason=detail[:64])

    # -- preempt / hand-over ---------------------------------------------------

    def request_preempt(self, reason: str, *, handover: bool = True) -> None:
        """Non-blocking entry for the lock hook: flags the abort and runs the preempt in
        its own task (the lock grant must never wait for the agent)."""
        self._signal_abort()
        self._spawn(self.preempt(reason, handover=handover))

    def _signal_abort(self) -> None:
        """Abort a start sequence in flight. A running preempt/hand-over is left alone."""
        if self._starting:
            self._abort = True
            self._cancel_pending()

    async def preempt(self, reason: str, *, handover: bool) -> None:
        if self._gone or not await self._current():
            return
        self._signal_abort()
        async with self._lock:
            try:
                await self._preempt_locked(reason, handover=handover)
            finally:
                self._abort = False

    async def _preempt_locked(self, reason: str, *, handover: bool) -> None:
        run = await self._open_run()
        if run is None:
            return
        decoder_may_run = run.state in (State.RUNNING, State.AGENT_OFFLINE)
        await self._set_state(run, State.STOPPING)
        await self._publish(run_state=State.STOPPING, slot=run.slot)
        self._abort = False  # our own best-effort commands below must go out
        try:
            if (decoder_may_run or self._decoder_start_sent) and not self._gone:
                try:
                    await self._request(
                        {"v": 1, "type": "decoder_stop", "decoder": SERVICE, "slot": int(run.slot)}
                    )
                except (_CommandFailedError, _AbortedError) as exc:
                    logger.info(
                        "passive: decoder_stop failed (%s), station %s", exc, self.station_id
                    )
            if handover:
                await self._apply_handover(run)
        finally:
            # Whatever went wrong above: never leave the run in STOPPING.
            await self._close(run, State.STOPPED, reason)
            self._reset_run_memory()
        await self._audit(
            f"system:passive_services stopped {SERVICE} on slot {run.slot} ({reason})"
        )
        await self._publish()

    async def _apply_handover(self, run) -> None:
        plan = await database_sync_to_async(self._handover_plan)(run)
        if plan is None:
            return
        module, values = plan
        for cap, value in profiles.profile_commands(values, module):
            if self._gone or WILDCARD_TOUCH in self._user_touched:
                return  # a user started a scan / took the radio over: it is theirs now
            if (module.slot, module.module_id, cap) in self._user_touched:
                continue  # a user already set this since they took the lock
            if await database_sync_to_async(self._ptt_keyed)():
                logger.info("passive: hand-over stopped, station is keyed (%s)", self.station_id)
                return
            try:
                await self._command(run.slot, module.module_id, cap, value)
            except (_CommandFailedError, _AbortedError) as exc:
                logger.info("passive: hand-over %s failed (%s)", cap, exc)

    # -- agent connection ------------------------------------------------------

    async def on_agent_disconnect(self) -> None:
        self._gone = True
        self._abort = True
        self._cancel_pending()
        async with self._lock:
            try:
                run = await self._open_run()
                if run is not None:
                    if run.state == State.RUNNING or self._decoder_start_sent:
                        await self._set_state(run, State.AGENT_OFFLINE)
                    else:
                        await self._close(run, State.STOPPED, "agent_disconnect")
                    self._protected = set()
            finally:
                self._abort = False
                self._decoder_start_sent = False

    async def shutdown(self) -> None:
        """Cancel own tasks and futures (consumer teardown)."""
        self._cancel_pending()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Wait for spawned tasks (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- reconcile with the agent's snapshot ----------------------------------

    async def _maybe_reconcile(self) -> None:
        if self._reconciled or self._snapshot is None or self._features is None:
            return
        self._reconciled = True
        run = await self._open_run()
        listed = {
            str(d.get("slot"))
            for d in self._snapshot
            if d.get("decoder") == SERVICE and is_slot(str(d.get("slot")))
        }
        if run is None:
            for slot in sorted(listed, key=int)[:MAX_ORPHAN_SLOTS]:
                self._spawn(self._stop_orphan(slot))
            return
        if run.slot not in listed:
            await self._close(run, State.STOPPED, "agent_restarted")
            self._reset_run_memory()
            await self._publish()
            return
        if run.state != State.RUNNING:
            await self._set_state(run, State.RUNNING)
        self._protected = await database_sync_to_async(self._protected_for)(run)
        reason = await self._reason(ignore_idle=True)
        decision = _PREEMPT_REASONS.get(reason)
        if decision is not None:
            self._spawn(self.preempt(decision[0], handover=decision[1]))
            return
        await self._publish(run_state=State.RUNNING, slot=run.slot)

    async def _stop_orphan(self, slot: str) -> None:
        async with self._lock:
            try:
                await self._request(
                    {"v": 1, "type": "decoder_stop", "decoder": SERVICE, "slot": int(slot)}
                )
            except (_CommandFailedError, _AbortedError) as exc:
                logger.info("passive: stopping orphan decoder failed (%s)", exc)

    # -- agent requests --------------------------------------------------------

    async def _request(self, frame: dict):
        """Send ``frame`` with a fresh ``passive-`` request id and await its ``result``."""
        if self._gone:
            raise _AbortedError
        rid = REQUEST_PREFIX + uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._futures[rid] = fut
        try:
            await self._send({**frame, "request_id": rid})
            result = await asyncio.wait_for(fut, self._timeout)
        except TimeoutError:
            raise _CommandFailedError("timeout") from None
        finally:
            self._futures.pop(rid, None)
        if result.get("ok") is not True:
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise _CommandFailedError(str(code or "failed"))
        return result

    async def _command(self, slot: str, module_id: str, capability: str, value) -> None:
        await self._request(
            {
                "v": 1,
                "type": "command",
                "slot": self._wire_slot(slot),
                "module": module_id,
                "capability": capability,
                "op": "set",
                "value": value,
            }
        )

    def _cancel_pending(self) -> None:
        for fut in list(self._futures.values()):
            if not fut.done():
                fut.set_exception(_AbortedError())

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("passive: task failed", exc_info=task.exception())

    def _bump_backoff(self) -> None:
        self._failures += 1
        self._retry_at = self._now() + timedelta(seconds=_backoff_seconds(self._failures))

    def _reset_run_memory(self) -> None:
        self._user_touched.clear()
        self._protected = set()
        self._decoder_start_sent = False

    # -- state publishing ------------------------------------------------------

    async def _publish(self, *, run_state=None, slot=None, reason=None) -> None:
        if run_state is None and reason in _QUIET_REASONS:
            reason = None  # a service nobody set up for this station stays silent
        payload = {
            "type": "passive_state",
            "service": SERVICE,
            "state": str(run_state) if run_state else "off",
            "slot": slot,
            "reason": reason,
        }
        key = (payload["state"], slot, reason)
        if key == self._published or (self._published is None and key[0] == "off" and not reason):
            return
        self._published = key
        await self._broadcast(payload)

    # -- sync DB helpers -------------------------------------------------------

    async def _open_run(self):
        return await database_sync_to_async(PassiveServiceRun.open_for)(self.station_id)

    async def _set_state(self, run, state) -> None:
        run.state = state
        await database_sync_to_async(PassiveServiceRun.objects.filter(pk=run.pk).update)(
            state=state
        )

    async def _close(self, run, state, end_reason: str, last_error: str = "") -> None:
        run.state, run.end_reason = state, end_reason
        await database_sync_to_async(
            PassiveServiceRun.objects.filter(pk=run.pk, ended_at__isnull=True).update
        )(
            state=state,
            end_reason=end_reason,
            last_error=last_error[:256],
            ended_at=self._now(),
        )

    async def _audit(self, message: str) -> None:
        await database_sync_to_async(StationAuditLog.log)(
            station_id=self.station_id, event_type="passive_service", message=message
        )

    async def _reason(self, *, ignore_idle: bool = False) -> str | None:
        return await database_sync_to_async(self._reason_sync)(ignore_idle)

    def _reason_sync(self, ignore_idle: bool) -> str | None:
        cfg = PassiveServiceConfig.get(SERVICE)
        if not cfg.enabled:
            return "disabled"
        station = Station.objects.filter(pk=self.station_id).first()
        if station is None:
            return "offline"
        now = self._now()
        if ignore_idle:
            now += timedelta(minutes=cfg.idle_minutes + 1)
        if self._unsupported:
            return "agent_too_old"
        return ineligible_reason(
            station,
            cfg,
            now=now,
            agent_connected=self._features is not None,
            agent_features=self._features or set(),
        )

    def _ptt_keyed(self) -> bool:
        from apps.audio import gate

        station = Station.objects.filter(pk=self.station_id).first()
        return station is not None and gate.ptt_keyed(station)

    def _lock_is_free(self) -> bool:
        from apps.control.models import ControlLock

        return not ControlLock.objects.filter(
            station_id=self.station_id, scope="station", holder__isnull=False
        ).exists()

    def _create_run(self):
        station = Station.objects.get(pk=self.station_id)
        cfg = PassiveServiceConfig.get(SERVICE)
        values = dict(cfg.profile)
        module = pick_slot(station, Decimal(str(values.get("frequency"))))
        if module is None or not self._lock_is_free():
            return None
        try:
            run = PassiveServiceRun.objects.create(
                station=station,
                service=SERVICE,
                slot=module.slot,
                module_id=module.module_id,
                state=State.STARTING,
            )
        except Exception:  # unique open-run constraint: somebody else got there first
            logger.info("passive: run already open (station %s)", self.station_id, exc_info=True)
            return None
        return run, module, values

    def _handover_plan(self, run):
        module = self._module_of(run)
        if module is None:
            return None
        station = Station.objects.get(pk=self.station_id)
        base = HandoverProfile.get().values
        return module, profiles.handover_values(station, module, base)

    def _protected_for(self, run) -> set[tuple[str, str, str]]:
        module = self._module_of(run)
        if module is None:
            return set()
        values = PassiveServiceConfig.get(SERVICE).profile
        return {(module.slot, module.module_id, c) for c in profiles.profile_caps(values, module)}

    def _module_of(self, run):
        return StationModule.objects.filter(
            station_id=self.station_id, slot=run.slot, module_id=run.module_id
        ).first()


async def preempt_hook(station, holder, reason) -> None:
    """Lock-acquire hook: tell the station's agent consumer to hand the radio over. Only a
    group message — the lock grant never waits for the agent. Sent only while a run is open
    (a start sequence creates its run first and re-checks the lock before the decoder)."""
    if not await database_sync_to_async(PassiveServiceRun.open_for)(station.pk):
        return
    await get_channel_layer().group_send(
        f"control_{station.pk}_agent",
        {"type": "control.passive_preempt", "reason": reason},
    )
