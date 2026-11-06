# station_agent/scanner.py
"""Sequential carrier scanner (repeater-scanner spec §7).

One asyncio task owned by the :class:`~station_agent.broker.Broker`. It walks a server-resolved
entry list strictly one entry at a time (consecutive entries may sit on different modules):

1. apply the entry's ``rx/tx_frequency`` + ``rx/tx_tone`` (``Broker.apply_preset``),
2. poll the module's ``carrier`` telemetry every ``poll_s`` for ``listen_s`` — no carrier → next,
3. carrier → ``active`` (``tx_route`` follows the entry), carrier drops → ``hang`` for ``hang_s``;
   back within the hang time → ``active`` again, otherwise → next entry,
4. wrap at the end of the list.

Every device access (apply, carrier read) still goes through ``Broker._execute`` (slot lock +
process-wide devlock), so the scanner is just one more serialized opener of the control line.

Concurrency contract: every scanner step — device op, ``tx_route`` change, ``scan_state`` emit —
runs under ``_step_lock`` and re-checks ``_held``/``_stopping`` inside it. :meth:`hold` and
:meth:`stop` set their flag and then take the lock once, so when they return the in-flight step
has finished and no further step can start. That is what makes "held first, then the manual
change / PTT key is applied" (spec §7.4) a guarantee rather than a race: the scanner can never
retune a module after the operator's command.

Failure handling: an entry whose apply or carrier read fails is skipped; as many consecutive
failing entries as the list is long (a whole pass) stop the scan (``no_reachable_entries``). A
slot paused for a module firmware update (``updating``) stops the scan (``module_update``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from station_agent import protocol as proto

logger = logging.getLogger(__name__)

# Upper bounds on server-provided input (the server builds the list; be defensive anyway).
MAX_ENTRIES = 500
MAX_REF_LEN = 64
MAX_LABEL_LEN = 128
MAX_MODULE_LEN = 64
MAX_TONE_LEN = 16
LISTEN_RANGE_S = (0.2, 60.0)
HANG_RANGE_S = (0.0, 600.0)
# While parked on a carrier (active/hang) nothing short can be missed any more, so poll the
# control line slower there; the listen window keeps the fine poll_s (spec §7.3).
ACTIVE_POLL_MIN_S = 0.25

_SLOT_UPDATING = "updating"
_TUNING = ("rx_frequency", "tx_frequency", "rx_tone", "tx_tone")


class _HeldSignal(Exception):  # noqa: N818 — control-flow signal, not an error
    """The scanner was held (or is stopping) — park instead of taking another step."""


class _EntryFailedError(Exception):
    """The current entry could not be applied/read — skip it."""


class _StopSignal(Exception):  # noqa: N818 — control-flow signal, not an error
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_entries(raw) -> list[dict] | None:
    """Validate a server ``entries`` list; ``None`` if anything is malformed.

    Returns normalized copies (only the known keys; ``label`` defaults to ``""``).
    """
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
        return None
    out = []
    seen: set[str] = set()
    for e in raw:
        if not isinstance(e, dict):
            return None
        ref, label = e.get("ref"), e.get("label", "")
        slot, module = e.get("slot"), e.get("module")
        if not (isinstance(ref, str) and ref and len(ref) <= MAX_REF_LEN) or ref in seen:
            return None  # refs identify entries (tuned entry, list adoption): must be unique
        seen.add(ref)
        if not (isinstance(label, str) and len(label) <= MAX_LABEL_LEN):
            return None
        if not (isinstance(slot, int) and not isinstance(slot, bool)):
            return None
        if not (isinstance(module, str) and module and len(module) <= MAX_MODULE_LEN):
            return None
        if not (_is_num(e.get("rx_frequency")) and _is_num(e.get("tx_frequency"))):
            return None
        tones = (e.get("rx_tone"), e.get("tx_tone"))
        if not all(isinstance(t, str) and len(t) <= MAX_TONE_LEN for t in tones):
            return None
        out.append(
            {
                "ref": ref,
                "label": label,
                "slot": slot,
                "module": module,
                **{k: e[k] for k in _TUNING},
            }
        )
    return out


def clamp_window(value, bounds: tuple[float, float], default: float) -> float:
    if not _is_num(value):
        return default
    lo, hi = bounds
    return float(min(max(value, lo), hi))


class Scanner:
    def __init__(
        self,
        apply_preset,
        read_carrier,
        set_tx_route,
        send,
        *,
        sleep=asyncio.sleep,
        now=time.monotonic,
        poll_s: float = 0.1,
        ptt_active=None,
        paused=None,
        held_check_s: float = 1.0,
        ts=None,
    ):
        self._apply_preset = apply_preset  # async (slot, module, values) -> {cap: {ok, ...}}
        self._read_carrier = read_carrier  # async (slot, module) -> {ok, value|error}
        self._set_tx_route = set_tx_route  # async (slot, module) -> None
        self._send = send
        self._sleep = sleep
        self._now = now
        self._poll_s = poll_s
        # Any PTT keyed/being keyed on the station? Then the scan must not retune or re-route.
        self._ptt_active = ptt_active or (lambda: False)
        # Slots paused for a module firmware update (a drain on a scanned slot stops the scan).
        self._paused = paused or (lambda: ())
        self._held_check_s = held_check_s
        self._ts = ts or now
        self._step_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._reset()

    def _reset(self) -> None:
        self._scan_id: str | None = None
        self._entries: list[dict] = []
        self._pending: list[dict] | None = None
        self._listen_s = 2.0
        self._hang_s = 5.0
        self._index = 0
        # ref of the entry the radio is actually tuned to (survives list updates; looked up by
        # ref) and of the entry tx_route points at.
        self._tuned_ref: str | None = None
        self._routed_ref: str | None = None
        self._state: str | None = None
        self._last: tuple | None = None  # last emitted (state, index, ref)
        self._held = False
        # Bumped by every hold(). A visit captures it at start; a step of that visit after
        # any hold — even one already resumed/skipped while the runner slept — aborts it.
        self._hold_gen = 0
        self._visit_gen = 0
        self._stopping = False
        self._skip = False
        self._resume = asyncio.Event()
        self._fail_streak = 0

    # --- public API (control event loop only) ------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def scan_id(self) -> str | None:
        return self._scan_id

    def active_slots(self) -> set[int]:
        if not self.running:
            return set()
        slots = {e["slot"] for e in self._entries}
        if self._pending:
            slots |= {e["slot"] for e in self._pending}
        return slots

    async def start(self, scan_id: str, entries: list[dict], listen_s: float, hang_s: float):
        if self.running:
            await self.stop("replaced")
        self._reset()
        self._scan_id = scan_id
        self._entries = list(entries)
        self._listen_s = listen_s
        self._hang_s = hang_s
        if not self._entries:
            msg = self._stopped_frame("empty_scan_list")
            self._reset()
            await self._send_msg(msg)
            return
        self._task = asyncio.ensure_future(self._run())

    async def update(self, scan_id: str, entries: list[dict]) -> None:
        """Replace the list; adopted at the next step (never mid-entry)."""
        if not self.running or scan_id != self._scan_id:
            return
        if not entries:
            await self.stop("empty_scan_list")
            return
        self._pending = list(entries)

    async def control(self, scan_id: str, action: str) -> None:
        if not self.running or scan_id != self._scan_id:
            return
        if action == "stop":
            await self.stop("operator")
        elif action == "hold":
            await self.hold("operator")
        elif action == "resume":
            if self._held:
                self._held = False
                self._resume.set()
        elif action == "skip":
            if self._held:
                # Skipping a held entry moves on and resumes scanning.
                self._index = self._next_index(1)
                self._held = False
                self._resume.set()
            else:
                self._skip = True
        else:
            logger.debug("scanner: ignoring unknown action %r", action)

    async def hold(self, reason: str, keyed: tuple[int, str] | None = None) -> None:
        """Park on the current entry. Returns once no scanner step is in flight.

        ``keyed`` = the (slot, module) a PTT is about to key: tx_route is then only moved to
        the tuned entry if that is the keyed module — never away from a module being keyed.
        """
        if not self.running or self._held or self._stopping:
            return
        self._held = True
        self._hold_gen += 1
        self._resume.clear()
        # Waits out the in-flight step (the next one sees the hold and aborts its visit). The
        # held frame and tx_route change run under the lock too, so a stop() can never cancel
        # the runner midway through them (the runner itself calls hold() on a keyed slot).
        async with self._step_lock:
            if not self.running or self._stopping:
                return
            # Between entries (_index already advanced, or a keyed slot refused the next
            # apply) the radio is still on the last tuned entry: hold there.
            pos = self._tuned_pos()
            if pos is not None:
                self._index = pos
            # Nothing tuned yet (scan started while keyed) or the last apply failed: the held
            # frame names no entry rather than one the radio is not on.
            await self._emit("held", reason=reason, untuned=pos is None)
            # Held on an entry that is actually tuned → PTT must key that module (§7.4).
            if pos is not None and self._routed_ref != self._tuned_ref:
                entry = self._entries[pos]
                target = (entry["slot"], entry["module"])
                # Never move the mic away from a module that is (being) keyed.
                if keyed == target or (keyed is None and not self._ptt_active()):
                    await self._route(entry)

    async def stop(self, reason: str) -> None:
        task = self._task
        if task is None or self._stopping:
            return
        self._stopping = True
        async with self._step_lock:
            # No step is in flight now (the runner is sleeping, parked, or queued on the
            # lock), so cancelling never interrupts a device op or a frame send midway.
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("scanner: task failed while stopping")
        self._task = None
        await self._send_stopped(reason)

    # --- runner ------------------------------------------------------------------------
    async def _run(self) -> None:
        try:
            while True:
                if self._held:
                    await self._wait_resume()
                    self._index = self._next_index(0)
                    continue
                if self._stopping:
                    return
                entry = self._entries[self._index]
                self._skip = False
                try:
                    reachable = await self._visit(entry)
                except _HeldSignal:
                    continue
                if reachable:
                    self._fail_streak = 0
                else:
                    self._fail_streak += 1
                    if self._fail_streak >= len(self._entries):
                        raise _StopSignal("no_reachable_entries")
                self._index = self._next_index(1)
        except _StopSignal as exc:
            await self._finish(exc.reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never leave a zombie scan behind
            logger.exception("scanner: scan %s crashed", self._scan_id)
            await self._finish("internal_error")

    async def _wait_resume(self) -> None:
        """Park until resumed; a module firmware drain on a scanned slot stops the scan."""
        while not self._resume.is_set():
            if self._drained():
                raise _StopSignal("module_update")
            try:
                await asyncio.wait_for(self._resume.wait(), self._held_check_s)
            except TimeoutError:
                pass

    def _drained(self) -> bool:
        return bool(set(self._paused()) & self.active_slots())

    async def _finish(self, reason: str) -> None:
        """Self-termination from inside the runner (stop() would cancel ourselves)."""
        if self._stopping:
            return  # an external stop() owns the shutdown
        self._task = None
        await self._send_stopped(reason)

    def _next_index(self, offset: int) -> int:
        """Index of the next entry; adopts a pending list update at this step boundary.

        With an update, the scan continues relative to the current entry's position in the
        new list (``offset`` 1 = the one after it, 0 = itself); if the current entry is gone
        it continues at the same position.
        """
        if self._pending is not None:
            cur_ref = self._entries[self._index]["ref"] if self._entries else None
            new, self._pending = self._pending, None
            pos = next((i for i, e in enumerate(new) if e["ref"] == cur_ref), None)
            self._entries = new
            self._fail_streak = 0
            if pos is None:
                return self._index % len(new)
            return (pos + offset) % len(new)
        return (self._index + offset) % len(self._entries)

    async def _visit(self, entry: dict) -> bool:
        """Scan one entry. True = it was reachable (whatever the carrier did)."""
        self._visit_gen = self._hold_gen
        await self._hold_if_keyed()
        await self._step(self._emit, "scanning")
        values = {k: entry[k] for k in _TUNING}
        results = await self._step(self._apply_preset, entry["slot"], entry["module"], values)
        # Set synchronously after the apply step, so hold() (which waits for the step) always
        # sees the outcome. A failed/partial apply leaves the radio on no known entry.
        ok = isinstance(results, dict) and bool(results)
        failed = [r for r in results.values() if not r.get("ok")] if ok else []
        self._tuned_ref = entry["ref"] if ok and not failed else None
        if not ok:
            return False
        if any((r.get("error") or {}).get("code") == _SLOT_UPDATING for r in failed):
            raise _StopSignal("module_update")
        if failed:
            logger.info("scanner: skipping %s (apply failed: %s)", entry["ref"], failed)
            return False
        try:
            # Listen window: poll continuously so a short over is not missed.
            deadline = self._now() + self._listen_s
            while True:
                if self._skip:
                    return True
                if await self._carrier(entry):
                    break
                if self._now() >= deadline:
                    return True
                await self._sleep(self._poll_s)
            await self._hold_if_keyed()
            await self._step(self._route, entry)
            await self._step(self._emit, "active")
            hang_end = None
            while True:
                if self._skip:
                    return True
                await self._sleep(max(self._poll_s, ACTIVE_POLL_MIN_S))
                if await self._carrier(entry):
                    if hang_end is not None:
                        hang_end = None
                        await self._step(self._emit, "active")
                    continue
                if hang_end is None:
                    hang_end = self._now() + self._hang_s
                    await self._step(self._emit, "hang")
                elif self._now() >= hang_end:
                    return True
        except _EntryFailedError:
            logger.info("scanner: skipping %s (carrier read failed)", entry["ref"])
            return False

    async def _carrier(self, entry: dict) -> bool:
        res = await self._step(self._read_carrier, entry["slot"], entry["module"])
        if not isinstance(res, dict) or not res.get("ok"):
            if isinstance(res, dict) and res.get("error") == _SLOT_UPDATING:
                raise _StopSignal("module_update")
            raise _EntryFailedError
        return res.get("value") is True

    async def _hold_if_keyed(self) -> None:
        # A PTT keyed anywhere (e.g. still keyed when the operator resumed): retuning or
        # moving tx_route now would change the transmitter under the operator (§7.4).
        if self._ptt_active():
            await self.hold("ptt")
            raise _HeldSignal

    async def _step(self, fn, *args):
        async with self._step_lock:
            if self._held or self._stopping or self._visit_gen != self._hold_gen:
                raise _HeldSignal
            if self._drained():
                raise _StopSignal("module_update")
            return await fn(*args)

    async def _route(self, entry: dict) -> None:
        try:
            await self._set_tx_route(entry["slot"], entry["module"])
        except Exception:  # noqa: BLE001 — routing is best effort; the scan goes on
            logger.exception("scanner: setting tx_route to %s failed", entry["ref"])
        self._routed_ref = entry["ref"]

    def _tuned_pos(self) -> int | None:
        if self._tuned_ref is None:
            return None
        return next((i for i, e in enumerate(self._entries) if e["ref"] == self._tuned_ref), None)

    # --- frames ------------------------------------------------------------------------
    async def _emit(self, state: str, *, reason: str | None = None, untuned: bool = False) -> None:
        entry = self._entries[self._index] if self._entries and not untuned else None
        ref = entry["ref"] if entry else None
        self._state = state
        if (state, self._index, ref) == self._last and reason is None:
            return
        self._last = (state, self._index, ref)
        await self._send_frame(state, entry, reason)

    async def _send_stopped(self, reason: str) -> None:
        """Snapshot the terminal frame and reset BEFORE awaiting the send: a scan_start that
        runs while the send is suspended must not have its fresh state wiped afterwards."""
        msg = self._stopped_frame(reason)
        self._reset()
        await self._send_msg(msg)

    def _stopped_frame(self, reason: str) -> dict | None:
        # The module stays where it last was (spec §7.5): name the tuned entry, not _index
        # (already advanced between entries), and no entry if nothing is tuned.
        pos = self._tuned_pos()
        if pos is not None:
            self._index = pos
        entry = self._entries[pos] if pos is not None else None
        return self._frame("stopped", entry, reason)

    async def _send_frame(self, state: str, entry: dict | None, reason: str | None) -> None:
        await self._send_msg(self._frame(state, entry, reason))

    def _frame(self, state: str, entry: dict | None, reason: str | None) -> dict | None:
        if self._scan_id is None:
            return None
        return proto.build_scan_state(
            self._scan_id,
            state,
            self._index,
            entry["ref"] if entry else None,
            entry["slot"] if entry else None,
            entry["module"] if entry else None,
            self._ts(),
            reason=reason,
        )

    async def _send_msg(self, msg: dict | None) -> None:
        if msg is None:
            return
        try:
            await self._send(msg)
        except Exception:  # noqa: BLE001 — a dead link must not kill the scan task
            logger.exception("scanner: sending scan_state failed")
