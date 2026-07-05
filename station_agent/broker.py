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


class Broker:
    def __init__(
        self,
        send,
        *,
        transport_factory=SlotControl,
        dead_man_timeout: float = 1.5,
        telemetry_default_interval_ms: int = 1000,
        telemetry_min_floor_ms: int = 200,
        now=time.monotonic,
    ):
        self._send = send
        self._transport_factory = transport_factory
        self._dead_man_timeout = dead_man_timeout
        self._telemetry_default_interval_ms = telemetry_default_interval_ms
        self._telemetry_min_floor_ms = telemetry_min_floor_ms
        self._now = now
        # (slot, module) -> descriptor dict; slot -> control path
        self._descriptors: dict[tuple[int, str], dict] = {}
        self._controls: dict[int, str] = {}

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
        else:
            logger.debug("broker: ignoring message type %r", mtype)

    async def handle_command(self, msg: dict) -> None:
        request_id = msg.get("request_id")
        slot = msg.get("slot")
        module = msg.get("module")
        capability = msg.get("capability")
        op = msg.get("op")
        value = msg.get("value")

        descriptor = self._descriptor(slot, module)
        if descriptor is None:
            code = proto.UNKNOWN_SLOT if slot not in self._controls else proto.UNKNOWN_MODULE
            await self._send(proto.build_result(request_id, False, error=(code, f"{slot}/{module}")))
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
        else:
            err_code = result.get("error", proto.TIMEOUT)
            await self._send(proto.build_result(request_id, False, error=(err_code, "")))

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
                state = {}
                for cap in caps:
                    if cap.get("kind") != "setting":
                        continue
                    result = await self._execute(slot, module, "get", cap["name"], None)
                    if result.get("ok"):
                        state[cap["name"]] = result.get("value")
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
        return await loop.run_in_executor(
            None, transport.execute, module, op, capability, token
        )
