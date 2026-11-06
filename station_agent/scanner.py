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
LISTEN_RANGE_S = (0.2, 60.0)
HANG_RANGE_S = (0.0, 600.0)

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
    for e in raw:
        if not isinstance(e, dict):
            return None
        ref, label = e.get("ref"), e.get("label", "")
        slot, module = e.get("slot"), e.get("module")
        if not (isinstance(ref, str) and ref and len(ref) <= MAX_REF_LEN):
            return None
        if not (isinstance(label, str) and len(label) <= MAX_LABEL_LEN):
            return None
        if not (isinstance(slot, int) and not isinstance(slot, bool)):
            return None
        if not (isinstance(module, str) and module):
            return None
        if not (_is_num(e.get("rx_frequency")) and _is_num(e.get("tx_frequency"))):
            return None
        if not (isinstance(e.get("rx_tone"), str) and isinstance(e.get("tx_tone"), str)):
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
        busy=None,
        ts=None,
    ):
        self._apply_preset = apply_preset  # async (slot, module, values) -> {cap: {ok, ...}}
        self._read_carrier = read_carrier  # async (slot, module) -> {ok, value|error}
        self._set_tx_route = set_tx_route  # async (slot, module) -> None
        self._send = send
        self._sleep = sleep
        self._now = now
        self._poll_s = poll_s
        self._busy = busy or (lambda slot: False)  # PTT keyed on slot?
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
        self._applied_index: int | None = None  # index whose settings are on the module
        self._routed_index: int | None = None  # index tx_route points at
        self._state: str | None = None
        self._last: tuple | None = None  # last emitted (state, index, ref)
        self._held = False
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
            await self._emit_stopped("empty_scan_list")
            self._reset()
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

    async def hold(self, reason: str) -> None:
        """Park on the current entry. Returns once no scanner step is in flight."""
        if not self.running or self._held or self._stopping:
            return
        self._held = True
        self._resume.clear()
        async with self._step_lock:
            pass  # wait out the in-flight step; the next one sees _held and parks
        if not self.running or self._stopping:
            return
        await self._emit("held", reason=reason)
        # Held on an entry that is actually tuned → PTT must key that module (spec §7.4).
        if self._applied_index == self._index and self._routed_index != self._index:
            await self._route(self._entries[self._index])

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
        await self._emit_stopped(reason)
        self._reset()

    # --- runner ------------------------------------------------------------------------
    async def _run(self) -> None:
        try:
            while True:
                if self._held:
                    await self._resume.wait()
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

    async def _finish(self, reason: str) -> None:
        """Self-termination from inside the runner (stop() would cancel ourselves)."""
        if self._stopping:
            return  # an external stop() owns the shutdown
        self._task = None
        await self._emit_stopped(reason)
        self._reset()

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
            self._applied_index = self._routed_index = None
            self._fail_streak = 0
            if pos is None:
                return self._index % len(new)
            return (pos + offset) % len(new)
        return (self._index + offset) % len(self._entries)

    async def _visit(self, entry: dict) -> bool:
        """Scan one entry. True = it was reachable (whatever the carrier did)."""
        if self._busy(entry["slot"]):
            await self.hold("ptt")
            raise _HeldSignal
        await self._step(self._emit, "scanning")
        values = {k: entry[k] for k in _TUNING}
        self._applied_index = None
        results = await self._step(self._apply_preset, entry["slot"], entry["module"], values)
        if not isinstance(results, dict):
            return False
        failed = [r for r in results.values() if not r.get("ok")]
        if any((r.get("error") or {}).get("code") == _SLOT_UPDATING for r in failed):
            raise _StopSignal("module_update")
        if failed or not results:
            logger.info("scanner: skipping %s (apply failed: %s)", entry["ref"], failed)
            return False
        self._applied_index = self._index
        self._routed_index = None
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
            await self._step(self._route, entry)
            await self._step(self._emit, "active")
            hang_end = None
            while True:
                if self._skip:
                    return True
                await self._sleep(self._poll_s)
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

    async def _step(self, fn, *args):
        async with self._step_lock:
            if self._held or self._stopping:
                raise _HeldSignal
            return await fn(*args)

    async def _route(self, entry: dict) -> None:
        try:
            await self._set_tx_route(entry["slot"], entry["module"])
        except Exception:  # noqa: BLE001 — routing is best effort; the scan goes on
            logger.exception("scanner: setting tx_route to %s failed", entry["ref"])
        self._routed_index = self._index

    # --- frames ------------------------------------------------------------------------
    async def _emit(self, state: str, *, reason: str | None = None) -> None:
        entry = self._entries[self._index] if self._entries else None
        ref = entry["ref"] if entry else None
        self._state = state
        if (state, self._index, ref) == self._last and reason is None:
            return
        self._last = (state, self._index, ref)
        await self._send_frame(state, entry, reason)

    async def _emit_stopped(self, reason: str) -> None:
        entry = None
        if self._entries and 0 <= self._index < len(self._entries):
            entry = self._entries[self._index]
        await self._send_frame("stopped", entry, reason)

    async def _send_frame(self, state: str, entry: dict | None, reason: str | None) -> None:
        if self._scan_id is None:
            return
        msg = proto.build_scan_state(
            self._scan_id,
            state,
            self._index,
            entry["ref"] if entry else None,
            entry["slot"] if entry else None,
            entry["module"] if entry else None,
            self._ts(),
            reason=reason,
        )
        try:
            await self._send(msg)
        except Exception:  # noqa: BLE001 — a dead link must not kill the scan task
            logger.exception("scanner: sending scan_state failed")
