"""Generic decoder host: lifecycle, health, spool and frame emission.

Lives in ``ControlClient`` (process lifetime), not in the per-connection ``Broker``:
decoders keep running while the control WebSocket is down; reliable output is spooled
and re-sent after reconnect (seq/ack). ``attach``/``detach`` bind the current WS sender.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from station_agent import protocol

from .spool import Spool
from .units import DecoderUnitError, DecoderUnits

logger = logging.getLogger(__name__)

# systemd is-active values that mean the unit is not (or no longer) running.
_DEAD_STATES = {"failed", "inactive", "deactivating"}


@dataclass
class _Instance:
    decoder: str
    slot: int
    state: str = "starting"
    reader_task: asyncio.Task | None = None
    health_task: asyncio.Task | None = None
    runtime_task: asyncio.Task | None = None
    stopping: bool = False


class DecoderHost:
    def __init__(
        self,
        units: DecoderUnits,
        plugins: dict,
        resolve_rx_node: Callable[[int], str | None],
        *,
        spool: Spool | None = None,
        loop_executor=None,
        now=time.time,
        sleep=asyncio.sleep,
        health_poll_s: float = 5.0,
    ):
        self._units = units
        self._plugins = plugins
        self._resolve = resolve_rx_node
        self._spool = spool or Spool()
        self._executor = loop_executor
        self._now = now
        self._sleep = sleep
        self._health_poll_s = health_poll_s
        self._send_fn: Callable[[dict], Awaitable[None]] | None = None
        self._instances: dict[int, _Instance] = {}  # one decoder per slot
        self._lock = asyncio.Lock()

    # -- connection binding -------------------------------------------------

    def attach(self, send: Callable[[dict], Awaitable[None]]) -> None:
        self._send_fn = send

    def detach(self) -> None:
        self._send_fn = None

    async def _send(self, msg: dict) -> None:
        send = self._send_fn
        if send is None:
            return
        try:
            await send(msg)
        except Exception:  # noqa: BLE001 - a dead socket must never break the host
            logger.debug("decoder frame send failed", exc_info=True)

    async def _blocking(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def _state(self, inst: _Instance, state: str, detail: str | None = None) -> None:
        await self._send(protocol.build_decoder_state(inst.decoder, inst.slot, state, detail))

    # -- lifecycle ------------------------------------------------------------

    def running(self) -> list[dict]:
        return [
            {"decoder": i.decoder, "slot": i.slot, "state": i.state}
            for i in sorted(self._instances.values(), key=lambda i: i.slot)
        ]

    async def startup(self) -> None:
        """Process start: stop leftover units (they outlive a crashed/restarted agent)."""
        await self._blocking(self._units.stop_all)

    async def start(
        self,
        decoder: str,
        slot: int,
        params: dict,
        request_id: str,
        max_runtime_s: float | None = None,
    ) -> None:
        async with self._lock:
            await self._start_locked(decoder, slot, params, request_id, max_runtime_s)

    async def _reject(self, request_id, decoder, slot, code: str, msg: str) -> None:
        await self._send(protocol.build_result(request_id, False, error=(code, msg)))
        await self._send(protocol.build_decoder_state(decoder, slot, "error", code))

    async def _start_locked(self, decoder, slot, params, request_id, max_runtime_s) -> None:
        plugin = self._plugins.get(decoder)
        if plugin is None:
            await self._reject(request_id, decoder, slot, "unknown_decoder", str(decoder))
            return
        existing = self._instances.get(slot)
        if existing is not None and existing.decoder == decoder:
            await self._send(protocol.build_result(request_id, True))
            return
        try:
            rx_node = await self._blocking(self._resolve, slot)
        except Exception:  # noqa: BLE001 - resolver talks to PipeWire; never crash the loop
            logger.warning("rx node resolve failed for slot %s", slot, exc_info=True)
            rx_node = None
        if not rx_node:
            await self._reject(request_id, decoder, slot, "no_rx_node", f"slot {slot}")
            return
        if existing is not None:
            await self._stop_locked(slot, "replaced")
        inst = _Instance(decoder, slot)
        await self._state(inst, "starting")
        try:
            env = plugin.env(slot, params or {}, rx_node)
            await self._blocking(self._units.start, decoder, slot, env)
        except DecoderUnitError as exc:
            await self._send(
                protocol.build_result(request_id, False, error=("unit_failed", str(exc)))
            )
            await self._state(inst, "error", f"unit_failed: {exc}")
            return
        self._instances[slot] = inst
        inst.reader_task = asyncio.create_task(self._reader(inst, plugin))
        inst.health_task = asyncio.create_task(self._health(inst))
        if max_runtime_s:
            inst.runtime_task = asyncio.create_task(self._max_runtime(inst, max_runtime_s))
        inst.state = "running"
        await self._send(protocol.build_result(request_id, True))
        await self._state(inst, "running")

    async def stop(
        self,
        decoder: str,
        slot: int,
        request_id: str | None = None,
        reason: str = "requested",
    ) -> None:
        async with self._lock:
            inst = self._instances.get(slot)
            if inst is None or inst.decoder != decoder:
                if request_id is not None:
                    await self._send(protocol.build_result(request_id, True))
                return
            err = await self._stop_locked(slot, reason)
        if request_id is not None:
            if err:
                await self._send(
                    protocol.build_result(request_id, False, error=("unit_failed", err))
                )
            else:
                await self._send(protocol.build_result(request_id, True))

    async def stop_slot(self, slot: int, reason: str) -> None:
        async with self._lock:
            await self._stop_locked(slot, reason)

    async def reconcile_slots(self, present: set[int]) -> None:
        for slot in [s for s in list(self._instances) if s not in present]:
            await self.stop_slot(slot, "module_gone")

    async def _stop_locked(self, slot: int, reason: str, *, announce: bool = True) -> str | None:
        inst = self._instances.pop(slot, None)
        if inst is None:
            return None
        inst.stopping = True
        current = asyncio.current_task()
        for task in (inst.reader_task, inst.health_task, inst.runtime_task):
            if task is not None and task is not current:
                task.cancel()
        for task in (inst.reader_task, inst.health_task, inst.runtime_task):
            if task is not None and task is not current:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.debug("decoder task ended with error", exc_info=True)
        err = None
        try:
            await self._blocking(self._units.stop, inst.decoder, inst.slot)
        except DecoderUnitError as exc:
            err = str(exc)
            logger.warning("stopping decoder %s slot %s failed: %s", inst.decoder, slot, exc)
        inst.state = "stopped"
        if announce:
            if err:
                await self._state(inst, "error", f"stop_failed: {err}")
            else:
                await self._state(inst, "stopped", reason)
        return err

    async def shutdown(self) -> None:
        async with self._lock:
            for slot in list(self._instances):
                await self._stop_locked(slot, "shutdown", announce=False)

    # -- reliable output --------------------------------------------------------

    async def ack(self, epoch: str, seqs: list[int]) -> None:
        self._spool.ack(epoch, seqs)

    async def on_connected(self) -> None:
        await self._send(protocol.build_decoder_snapshot(self.running()))
        for frame in self._spool.pending():
            await self._send(frame)

    async def _emit(self, inst: _Instance, kind: str, reliable: bool, data: dict) -> None:
        ts = self._now()
        if reliable:
            await self._send(self._spool.add(inst.decoder, inst.slot, kind, data, ts))
        elif self._send_fn is not None:
            frame = protocol.build_decoder_output(
                inst.decoder, inst.slot, self._spool.epoch, 0, kind, False, data, ts
            )
            await self._send(frame)

    # -- per-instance tasks -------------------------------------------------------

    async def _fail(self, inst: _Instance, detail: str) -> None:
        """Instance died on its own: report, stop the unit, forget it."""
        async with self._lock:
            if inst.stopping or self._instances.get(inst.slot) is not inst:
                return
            await self._stop_locked(inst.slot, detail, announce=False)
            await self._state(inst, "error", detail)

    async def _reader(self, inst: _Instance, plugin) -> None:
        async def emit(kind: str, reliable: bool, data: dict) -> None:
            await self._emit(inst, kind, reliable, data)

        try:
            await plugin.run(inst.slot, emit)
            detail = "reader ended"
        except asyncio.CancelledError:
            raise
        except ConnectionError as exc:
            detail = f"reader: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("decoder %s slot %s reader crashed", inst.decoder, inst.slot)
            detail = f"reader crashed: {type(exc).__name__}"
        await self._fail(inst, detail)

    async def _health(self, inst: _Instance) -> None:
        while True:
            await self._sleep(self._health_poll_s)
            state = await self._blocking(self._units.state, inst.decoder, inst.slot)
            if state in _DEAD_STATES:
                await self._fail(inst, f"unit {state}")
                return

    async def _max_runtime(self, inst: _Instance, seconds: float) -> None:
        await self._sleep(seconds)
        # Own task: stopping must not cancel the task that is doing the stopping.
        async with self._lock:
            if self._instances.get(inst.slot) is inst:
                await self._stop_locked(inst.slot, "max_runtime")
