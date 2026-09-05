"""AudioEngine — orchestrates the stream registry, RouterBackend, and Opus bridges.

The engine is transport-agnostic (mirrors how ``Broker`` takes a ``send`` callable): it is
handed an async ``emit_json`` and a sync ``emit_binary`` by the audio-WS client and never
imports websockets itself. Responsibilities (Spec 0 §5):

- **advertise** the source streams the station offers (demand-gated production).
- **source_subscribe / _unsubscribe** — start/stop an RX Opus bridge on demand; pump its
  packets as §5.3 binary media frames.
- **mic_state** — start/stop a TX bridge for the routed module; inject inbound ``op.mic``
  media; arm a local dead-man that tears TX down on silence (defence-in-depth beside the
  server lock/PTT gate and the control-plane PTT).
- **tx_route** — hold which module the operator mic transmits into.

RX bridges call ``on_opus`` from their reader thread; ``emit_binary`` must therefore be a
thread-safe hand-off (the WS client schedules the actual send on its loop).
"""

from __future__ import annotations

import asyncio
import logging

from station_agent.audio import frame
from station_agent.audio.bridge_factory import BridgeFactory
from station_agent.audio.streams import OP_MIC, StreamRegistry

logger = logging.getLogger(__name__)

# Opus DTX comfort-noise packets are ≤2 bytes; flag them so a receiver can treat the gap
# as comfort noise rather than loss.
_DTX_MAX_BYTES = 2


class AudioEngine:
    def __init__(
        self,
        backend,
        *,
        emit_json,
        emit_binary,
        bridge_factory=None,
        rx_rate: int = 8000,
        mic_rate: int = 16000,
        dead_man_timeout: float = 1.5,
    ):
        self._backend = backend
        self._emit_json = emit_json
        self._emit_binary = emit_binary
        self._factory = bridge_factory or BridgeFactory()
        self.registry = StreamRegistry(rx_rate=rx_rate, mic_rate=mic_rate)
        self._dead_man_timeout = dead_man_timeout
        # stream_id -> {"bridge": rx_bridge, "seq": int, "ts": int, "rate": int}
        self._rx: dict[str, dict] = {}
        self._tx = None  # {"bridge": tx_bridge, "slot": int, "module": str}
        self._dead_man: asyncio.Task | None = None
        self.tx_route: dict | None = None

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Discover audio slots, (re)build the stream set, and advertise."""
        self.registry.rebuild(self._backend.list_audio_slots())
        await self.advertise()

    async def advertise(self) -> None:
        await self._emit_json(self.registry.advertise_payload())

    async def stop(self) -> None:
        for stream_id in list(self._rx):
            await self.on_source_unsubscribe(stream_id)
        await self._teardown_tx()

    # --- RX (demand-gated source production) -------------------------------
    async def on_source_subscribe(self, stream_id: str) -> None:
        info = self.registry.get(stream_id)
        if info is None or stream_id == OP_MIC:
            # op.mic is browser-produced; the agent never encodes it as a source.
            await self._emit_error("unknown_stream", f"no producible source {stream_id!r}")
            return
        if stream_id in self._rx:
            return  # already producing; demand is server-tracked
        node = self._backend.resolve_node(info.slot, "rx")
        if node is None:
            await self._emit_error("unknown_stream", f"no RX node for {stream_id!r}")
            await self._emit_stream_state(stream_id, "error", "node unresolved")
            return
        state = {"seq": 0, "ts": 0, "rate": info.rate, "ref": info.stream_ref}

        def on_opus(payload: bytes, _st=state, _sid=stream_id) -> None:
            self._emit_media(_sid, _st, payload)

        bridge = self._factory.make_rx(node, info.rate, on_opus)
        state["bridge"] = bridge
        self._rx[stream_id] = state
        bridge.start()
        await self._emit_stream_state(stream_id, "live", "")

    async def on_source_unsubscribe(self, stream_id: str) -> None:
        state = self._rx.pop(stream_id, None)
        if state is None:
            return
        _safe_stop(state["bridge"])
        await self._emit_stream_state(stream_id, "idle", "")

    def _emit_media(self, stream_id: str, state: dict, payload: bytes) -> None:
        flags = frame.FLAG_DTX if len(payload) <= _DTX_MAX_BYTES else 0
        if state["seq"] == 0:
            flags |= frame.FLAG_MARKER  # talk-onset on the first packet after (re)subscribe
        data = frame.pack_frame(
            stream_ref=state["ref"],
            seq=state["seq"],
            ts=state["ts"],
            flags=flags,
            payload=payload,
        )
        state["seq"] += 1
        # ts advances by one 20 ms frame worth of samples at the stream rate.
        state["ts"] += state["rate"] * 20 // 1000
        self._emit_binary(data)

    # --- TX (mic → module) -------------------------------------------------
    async def on_mic_state(self, *, active: bool, tx_slot, tx_module) -> None:
        if not active:
            await self._teardown_tx()
            return
        node = self._backend.resolve_node(tx_slot, "tx")
        if node is None:
            await self._emit_error("unknown_stream", f"no TX node for slot {tx_slot}")
            return
        if self._tx is not None:
            await self._teardown_tx()
        bridge = self._factory.make_tx(node, self.registry.get(OP_MIC).rate)
        self._tx = {"bridge": bridge, "slot": tx_slot, "module": tx_module}
        bridge.start()
        self._arm_dead_man()

    async def on_media_frame(self, data: bytes) -> None:
        try:
            mf = frame.parse_frame(data)
        except frame.FrameError:
            logger.debug("engine: dropping malformed media frame")
            return
        if self._tx is None or mf.stream_ref != self.registry.mic_ref:
            return  # only op.mic media is injected, and only while TX is up
        self._tx["bridge"].feed_opus(mf.payload)
        self._arm_dead_man()  # pet the dead-man

    async def set_tx_route(self, slot, module) -> None:
        self.tx_route = None if slot is None else {"slot": slot, "module": module}

    # --- dead-man ----------------------------------------------------------
    def _arm_dead_man(self) -> None:
        self._disarm_dead_man()
        self._dead_man = asyncio.ensure_future(self._dead_man_loop())

    def _disarm_dead_man(self) -> None:
        if self._dead_man is not None:
            self._dead_man.cancel()
            self._dead_man = None

    async def _dead_man_loop(self) -> None:
        try:
            await asyncio.sleep(self._dead_man_timeout)
        except asyncio.CancelledError:
            raise
        logger.info("engine: mic dead-man fired — tearing down TX")
        await self._teardown_tx()

    async def _teardown_tx(self) -> None:
        self._disarm_dead_man()
        if self._tx is not None:
            _safe_stop(self._tx["bridge"])
            self._tx = None

    # --- helpers -----------------------------------------------------------
    async def _emit_stream_state(self, stream_id: str, state: str, detail: str) -> None:
        await self._emit_json(
            {
                "v": 1,
                "type": "stream_state",
                "stream_id": stream_id,
                "state": state,
                "detail": detail,
            }
        )

    async def _emit_error(self, code: str, detail: str) -> None:
        await self._emit_json({"v": 1, "type": "error", "code": code, "detail": detail})


def _safe_stop(bridge) -> None:
    try:
        bridge.stop()
    except Exception:  # noqa: BLE001 — teardown must not raise into the message loop
        logger.exception("engine: bridge stop raised")
