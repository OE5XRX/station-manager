# station_agent/broker.py
"""Device-agnostic broker: validate -> translate -> execute -> report.

The broker turns semantic ``(slot, module, capability, op, value)`` commands into
concrete generic firmware commands, validating each against the cached ``describe``
descriptor BEFORE the firmware. It never hardcodes a module id — everything is read
from the descriptors set via ``set_inventory``. Command pipeline lives here; telemetry
subscription and the PTT dead-man are layered on in later tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time

from station_agent import descriptor as desc
from station_agent import protocol as proto
from station_agent.slot_control import SlotControl

logger = logging.getLogger(__name__)


def _valid_addr(slot, module) -> bool:
    # slot is an int (not bool), module is a str — hashable and type-correct for keying.
    return isinstance(slot, int) and not isinstance(slot, bool) and isinstance(module, str)


def _as_str_list(value) -> list[str]:
    """Return a list of strings from *value*, silently dropping non-string items.

    A bare string is NOT treated as an iterable of characters — it returns [].
    Any non-list value (int, None, …) returns [].
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


class Broker:
    def __init__(
        self,
        send,
        *,
        transport_factory=None,
        slot_command_timeout: float = 5.0,
        dead_man_timeout: float = 1.5,
        telemetry_default_interval_ms: int = 1000,
        telemetry_min_floor_ms: int = 200,
        trace_serial: bool = False,
        virtual_modules=None,
        now=time.monotonic,
    ):
        self._send = send
        # Virtual modules (e.g. the audio-router, Spec 0 §5.6) present a descriptor + their
        # own command handler on the existing control-plane. Keyed by (slot, module_id).
        # Empty by default ⇒ byte-identical behaviour to the physical-only broker.
        self._virtual = {(vm.slot, vm.module_id): vm for vm in (virtual_modules or [])}
        # Hex-dump every control-path TX/RX (SlotControl.execute) when tracing is
        # on, so `trace_serial` covers BOTH serial paths (discovery + control),
        # not just discovery.
        self._trace = trace_serial
        # A command's whole slot round-trip must outlast the module's worst-case
        # firmware timeout, or SlotControl gives up first and the real device
        # error (e.g. driver_error from an unanswered SA818 AT command, ~2 s)
        # surfaces as a generic "timeout" instead. Keep it comfortably above the
        # firmware AT timeout and well below the server's command timeout (10 s):
        #   firmware AT (~2 s)  <  slot_command_timeout (5 s)  <  server (10 s).
        self._slot_command_timeout = slot_command_timeout
        # Default transport bakes the configured timeout into SlotControl. An
        # explicit transport_factory (tests) is a single-arg (path) callable and
        # is used verbatim, so this never changes its arity.
        self._transport_factory = transport_factory or (
            lambda path: SlotControl(path, timeout=slot_command_timeout)
        )
        self._dead_man_timeout = dead_man_timeout
        self._telemetry_default_interval_ms = telemetry_default_interval_ms
        self._telemetry_min_floor_ms = telemetry_min_floor_ms
        self._now = now
        # (slot, module) -> descriptor dict; slot -> control path
        self._descriptors: dict[tuple[int, str], dict] = {}
        self._controls: dict[int, str] = {}
        # (slot, module) -> {"caps": set, "interval_s": float, "task": asyncio.Task | None}
        self._subscriptions: dict[tuple[int, str], dict] = {}
        # (slot, module) -> {"cap": capability_name, "task": asyncio.Task}
        self._ptt: dict[tuple[int, str], dict] = {}
        # slot -> asyncio.Lock serializing device access. A slot's control device
        # is a single serial/pty line: concurrent execute() calls (e.g. a telemetry
        # poll tick and a command) would interleave their write/read framing on the
        # wire and corrupt each other's MODULE-RESULT. One lock per slot makes every
        # command+telemetry access to a given slot mutually exclusive.
        self._slot_locks: dict[int, asyncio.Lock] = {}

    # --- inventory cache ---------------------------------------------------
    def set_inventory(self, discovered: list) -> None:
        self._descriptors.clear()
        self._controls.clear()
        for slot_entry in discovered:
            slot = slot_entry.get("slot")
            self._controls[slot] = slot_entry.get("control", "")
            for module in slot_entry.get("modules", []):
                mid = module.get("id")
                self._descriptors[(slot, mid)] = {
                    "identity": module.get("identity", {}),
                    "capabilities": module.get("capabilities", []),
                }
        # Register virtual modules alongside the discovered physical ones so they appear in
        # inventory and command routing. They carry no control device.
        for (slot, mid), vm in self._virtual.items():
            self._descriptors[(slot, mid)] = vm.descriptor()

    def _descriptor(self, slot, module) -> dict | None:
        return self._descriptors.get((slot, module))

    def _control_path(self, slot) -> str | None:
        return self._controls.get(slot)

    def _ts(self) -> float:
        return self._now()

    # --- dispatch ----------------------------------------------------------
    async def handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "command":
            await self.handle_command(msg)
        elif mtype == "subscribe":
            await self.handle_subscribe(msg)
        elif mtype == "unsubscribe":
            await self.handle_unsubscribe(msg)
        elif mtype == "ptt_keepalive":
            await self.handle_keepalive(msg)
        else:
            logger.debug("broker: ignoring message type %r", mtype)

    async def handle_command(self, msg: dict) -> None:
        request_id = msg.get("request_id")
        # Fix 1: fail closed on missing/invalid request_id (spec §7).
        # A valid correlation token is a non-empty string or a non-bool integer.
        _rid_valid = (isinstance(request_id, str) and request_id) or (
            isinstance(request_id, int) and not isinstance(request_id, bool)
        )
        if not _rid_valid:
            logger.debug("broker: dropping command with invalid request_id %r", request_id)
            return

        slot = msg.get("slot")
        module = msg.get("module")
        capability = msg.get("capability")
        op = msg.get("op")
        value = msg.get("value")

        if (
            not _valid_addr(slot, module)
            or not isinstance(capability, str)
            or not isinstance(op, str)
        ):
            await self._send(
                proto.build_result(
                    request_id, False, error=(proto.VALIDATION_FAILED, "malformed command frame")
                )
            )
            return

        descriptor = self._descriptor(slot, module)
        if descriptor is None:
            code = proto.UNKNOWN_SLOT if slot not in self._controls else proto.UNKNOWN_MODULE
            await self._send(
                proto.build_result(request_id, False, error=(code, f"{slot}/{module}"))
            )
            return

        # Virtual modules validate + execute their own commands (their value types are not
        # the physical FW token vocabulary), so route to them before the descriptor path.
        vm = self._virtual.get((slot, module))
        if vm is not None:
            await self._handle_virtual_command(request_id, slot, module, vm, op, capability, value)
            return

        caps = desc.index_capabilities(descriptor)
        cap = caps.get(capability)
        try:
            desc.validate_command(cap, op, value)
        except proto.ProtocolError as exc:
            await self._send(proto.build_result(request_id, False, error=(exc.code, exc.msg)))
            return

        token = None
        if op != "get":
            token = desc.format_value(cap["type"], value)

        result = await self._execute(slot, module, op, capability, token)
        if result.get("ok"):
            await self._send(proto.build_result(request_id, True, value=result.get("value")))
            await self._send(
                proto.build_state(slot, module, {capability: result.get("value")}, self._ts())
            )
            if op == "do" and self._is_ptt_cap(cap):
                if value is True:
                    self._arm_dead_man(slot, module, capability)
                elif value is False:
                    self._disarm_dead_man(slot, module)
        else:
            err_code = result.get("error", proto.TIMEOUT)
            await self._send(proto.build_result(request_id, False, error=(err_code, "")))

    async def _handle_virtual_command(
        self, request_id, slot, module, vm, op, capability, value
    ) -> None:
        try:
            result = await vm.handle_command(op, capability, value)
        except Exception:  # noqa: BLE001 — a virtual handler must not break the control link
            logger.exception("broker: virtual module %s/%s handler raised", slot, module)
            await self._send(
                proto.build_result(
                    request_id, False, error=(proto.VALIDATION_FAILED, "handler error")
                )
            )
            return
        if result.get("ok"):
            await self._send(proto.build_result(request_id, True, value=result.get("value")))
            await self._send(
                proto.build_state(slot, module, {capability: result.get("value")}, self._ts())
            )
        else:
            err = result.get("error", (proto.VALIDATION_FAILED, ""))
            code, emsg = err if isinstance(err, tuple) else (err, "")
            await self._send(proto.build_result(request_id, False, error=(code, emsg)))

    async def emit_inventory(self) -> None:
        slots_out = []
        # Deterministic order: sort by slot number.
        by_slot: dict[int, list] = {}
        for (slot, module), descriptor in self._descriptors.items():
            by_slot.setdefault(slot, []).append((module, descriptor))
        for slot in sorted(by_slot):
            modules_out = []
            for module, descriptor in by_slot[slot]:
                caps = descriptor.get("capabilities", [])
                if not isinstance(caps, list):
                    caps = []
                vm = self._virtual.get((slot, module))
                if vm is not None:
                    # Virtual modules have no control device; snapshot their own state
                    # instead of polling a serial line.
                    modules_out.append(
                        {
                            "module": module,
                            "identity": descriptor.get("identity", {}),
                            "capabilities": caps,
                            "state": vm.state(),
                        }
                    )
                    continue
                state = {}
                for cap in caps:
                    if not isinstance(cap, dict):
                        continue
                    if cap.get("kind") != "setting":
                        continue
                    name = cap.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    result = await self._execute(slot, module, "get", name, None)
                    if result.get("ok"):
                        state[name] = result.get("value")
                modules_out.append(
                    {
                        "module": module,
                        "identity": descriptor.get("identity", {}),
                        "capabilities": caps,
                        "state": state,
                    }
                )
            slots_out.append({"slot": slot, "modules": modules_out})
        await self._send(proto.build_inventory(slots_out))

    async def _execute(self, slot, module, op, capability, token) -> dict:
        transport = self._transport_factory(self._control_path(slot))
        loop = asyncio.get_running_loop()
        lock = self._slot_locks.get(slot)
        if lock is None:
            lock = asyncio.Lock()
            self._slot_locks[slot] = lock
        # Serialize per-slot device I/O so a telemetry poll and a command never
        # interleave their framing on the same serial/pty line.
        async with lock:
            return await loop.run_in_executor(
                None, transport.execute, module, op, capability, token, self._trace
            )

    # --- telemetry subscription --------------------------------------------
    def _poll_interval_s(self, slot, module) -> float | None:
        sub = self._subscriptions.get((slot, module))
        return sub["interval_s"] if sub else None

    def _telemetry_caps(self, slot, module, requested):
        descriptor = self._descriptor(slot, module)
        if descriptor is None:
            return {}, self._telemetry_min_floor_ms / 1000.0
        caps = desc.index_capabilities(descriptor)
        valid, min_interval_ms = {}, self._telemetry_min_floor_ms
        for name in requested:
            cap = caps.get(name)
            if cap is None or cap.get("kind") != "telemetry":
                logger.debug("broker: ignoring non-telemetry subscribe cap %r", name)
                continue
            valid[name] = cap
            min_interval_ms = max(
                min_interval_ms, desc.min_interval_ms(cap, self._telemetry_default_interval_ms)
            )
        return valid, min_interval_ms / 1000.0

    async def handle_subscribe(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        if not _valid_addr(slot, module):
            logger.debug(
                "broker: ignoring subscribe with malformed addr slot=%r module=%r", slot, module
            )
            return
        requested = _as_str_list(msg.get("capabilities"))
        raw_interval = msg.get("interval_ms")
        if (
            isinstance(raw_interval, bool)
            or not isinstance(raw_interval, (int, float))
            or raw_interval <= 0
        ):
            interval_s = self._telemetry_default_interval_ms / 1000.0
        else:
            interval_s = raw_interval / 1000.0

        valid, _ = self._telemetry_caps(slot, module, requested)
        if not valid:
            return  # nothing pollable; no subscriber => no poll

        key = (slot, module)
        existing = self._subscriptions.get(key)
        caps = set(valid) | (existing["caps"] if existing else set())

        # Clamp over the FULL merged cap set (spec §6: max across all subscribed caps).
        # Store the raw requested_s (before clamp) so unsubscribe can recompute correctly.
        _, min_interval_s = self._telemetry_caps(slot, module, caps)
        effective = max(interval_s, min_interval_s)
        if existing and existing["task"] is not None:
            existing["task"].cancel()
            try:
                await existing["task"]
            except (asyncio.CancelledError, Exception):
                pass
        task = asyncio.ensure_future(self._poll_loop(slot, module, effective))
        self._subscriptions[key] = {
            "caps": caps,
            "requested_s": interval_s,
            "interval_s": effective,
            "task": task,
        }

    async def handle_unsubscribe(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        if not _valid_addr(slot, module):
            logger.debug(
                "broker: ignoring unsubscribe with malformed addr slot=%r module=%r", slot, module
            )
            return
        key = (slot, module)
        sub = self._subscriptions.get(key)
        if not sub:
            return
        sub["caps"] -= set(_as_str_list(msg.get("capabilities")))
        if sub["task"] is not None:
            sub["task"].cancel()
            try:
                await sub["task"]
            except (asyncio.CancelledError, Exception):
                pass
        if sub["caps"]:
            # Fix 2+3: recompute effective interval over the REMAINING cap set (spec §6).
            _, min_interval_s = self._telemetry_caps(slot, module, sorted(sub["caps"]))
            effective = max(sub["requested_s"], min_interval_s)
            sub["interval_s"] = effective
            sub["task"] = asyncio.ensure_future(self._poll_loop(slot, module, effective))
        else:
            del self._subscriptions[key]

    async def _poll_loop(self, slot, module, interval_s: float) -> None:
        try:
            while True:
                sub = self._subscriptions.get((slot, module))
                if not sub or not sub["caps"]:
                    return
                values = {}
                for cap_name in sorted(sub["caps"]):
                    try:
                        result = await self._execute(slot, module, "get", cap_name, None)
                    except Exception:
                        logger.exception(
                            "broker: execute error in telemetry poll for slot %s module %s cap %s",
                            slot,
                            module,
                            cap_name,
                        )
                        continue
                    if result.get("ok"):
                        values[cap_name] = result.get("value")
                if values:
                    await self._send(proto.build_state(slot, module, values, self._ts()))
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("broker: telemetry poll failed for slot %s module %s", slot, module)

    # --- PTT dead-man ----------------------------------------------------------
    @staticmethod
    def _is_ptt_cap(cap: dict) -> bool:
        # Generic: a bool action named 'ptt' in the platform vocabulary — no module id.
        return (
            cap.get("kind") == "action" and cap.get("type") == "bool" and cap.get("name") == "ptt"
        )

    def _arm_dead_man(self, slot, module, capability) -> None:
        self._disarm_dead_man(slot, module)
        task = asyncio.ensure_future(self._dead_man(slot, module, capability))
        self._ptt[(slot, module)] = {"cap": capability, "task": task}

    def _disarm_dead_man(self, slot, module) -> None:
        entry = self._ptt.pop((slot, module), None)
        if entry and entry["task"] is not None:
            entry["task"].cancel()

    async def _dead_man(self, slot, module, capability) -> None:
        try:
            await asyncio.sleep(self._dead_man_timeout)
        except asyncio.CancelledError:
            raise
        self._ptt.pop((slot, module), None)
        await self._unkey(slot, module, capability, "keepalive_timeout")

    async def handle_keepalive(self, msg: dict) -> None:
        slot, module = msg.get("slot"), msg.get("module")
        if not _valid_addr(slot, module):
            logger.debug(
                "broker: ignoring keepalive with malformed addr slot=%r module=%r", slot, module
            )
            return
        entry = self._ptt.get((slot, module))
        if not entry:
            return  # nothing keyed — keepalive is a no-op
        self._arm_dead_man(slot, module, entry["cap"])

    async def _unkey(self, slot, module, capability, reason) -> None:
        # Fail-safe: try to drive the module low, then always announce it.
        await self._execute(slot, module, "do", capability, "false")
        await self._send(proto.build_event(slot, module, "ptt_auto_unkey", {"reason": reason}))

    async def on_disconnect(self) -> None:
        cancelled_tasks = []
        for (slot, module), entry in list(self._ptt.items()):
            if entry["task"] is not None:
                entry["task"].cancel()
                cancelled_tasks.append(entry["task"])
            del self._ptt[(slot, module)]
            await self._unkey(slot, module, entry["cap"], "ws_disconnect")
        for task in cancelled_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.stop()

    async def stop(self) -> None:
        tasks = []
        for sub in list(self._subscriptions.values()):
            if sub["task"] is not None:
                sub["task"].cancel()
                tasks.append(sub["task"])
        self._subscriptions.clear()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Cancel any armed PTT dead-man timers (do NOT unkey — on_disconnect's job).
        ptt_tasks = []
        for entry in list(self._ptt.values()):
            if entry["task"] is not None:
                entry["task"].cancel()
                ptt_tasks.append(entry["task"])
        self._ptt.clear()
        for task in ptt_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
