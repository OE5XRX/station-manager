"""``audio-router`` virtual control-plane module (Spec 0 §5.6).

The router is not a new channel — it appears as a **virtual module** on the existing
control-plane so stream enumeration and TX routing are descriptor-driven like every other
capability (Spec 0 §8). This class plugs into :class:`station_agent.broker.Broker` through
its ``virtual_modules`` seam and provides:

- ``streams`` (telemetry): the dynamic list of available audio streams (mirrors ``advertise``).
- ``tx_route`` (action, ``op:set``): sets which module the operator mic transmits into,
  value ``{"slot":N,"module":"fm"}`` or ``null`` to clear. **Lock-gated by the server** —
  the agent executes what the (lock-enforcing) server forwards, mirroring every other write.

The authoritative mic-injection target reaches the agent via ``mic_state`` on the audio-WS
(the server derives it from the stored ``tx_route``); this handler records the route and acks,
so control and audio stay one declarative model without cross-thread coupling. An optional
``on_tx_route`` hook is left for the Spec 0 §11 non-human TX requester (not built now).
"""

from __future__ import annotations

import logging

from station_agent.protocol import BAD_VALUE, WRONG_OP

logger = logging.getLogger(__name__)

MODULE_ID = "audio-router"


class AudioRouterModule:
    def __init__(self, slot: int = 1000, *, list_streams=None, on_tx_route=None):
        # Synthetic slot far above any physical slot so it never collides.
        self.slot = slot
        self.module_id = MODULE_ID
        self._list_streams = list_streams or (lambda: [])
        self._on_tx_route = on_tx_route
        self.tx_route: dict | None = None

    def descriptor(self) -> dict:
        return {
            "identity": {"type": "audio_router", "virtual": True},
            "capabilities": [
                {"name": "streams", "kind": "telemetry", "type": "list", "readonly": True},
                {"name": "tx_route", "kind": "action", "type": "route"},
            ],
        }

    def state(self) -> dict:
        return {"streams": self._list_streams(), "tx_route": self.tx_route}

    async def handle_command(self, op: str, capability: str, value) -> dict:
        if capability == "tx_route":
            return self._set_tx_route(op, value)
        if capability == "streams":
            if op != "get":
                return {"ok": False, "error": (WRONG_OP, "streams is read-only")}
            return {"ok": True, "value": self._list_streams()}
        return {"ok": False, "error": (BAD_VALUE, f"unknown capability {capability!r}")}

    def _set_tx_route(self, op: str, value) -> dict:
        if op != "set":
            return {"ok": False, "error": (WRONG_OP, "tx_route accepts op 'set'")}
        route = self._validate_route(value)
        if route is _INVALID:
            return {
                "ok": False,
                "error": (BAD_VALUE, "tx_route value must be {slot,module} or null"),
            }
        self.tx_route = route
        if self._on_tx_route is not None:
            try:
                self._on_tx_route(route)
            except Exception:  # noqa: BLE001 — a hook error must not fail the command
                logger.exception("audio-router: on_tx_route hook raised")
        return {"ok": True, "value": route}

    @staticmethod
    def _validate_route(value):
        if value is None:
            return None
        if (
            isinstance(value, dict)
            and isinstance(value.get("slot"), int)
            and not isinstance(value.get("slot"), bool)
            and isinstance(value.get("module"), str)
            and value["module"]
        ):
            return {"slot": value["slot"], "module": value["module"]}
        return _INVALID


_INVALID = object()
