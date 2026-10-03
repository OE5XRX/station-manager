"""AudioEngine — orchestrates the stream registry, RouterBackend, and Opus bridges.

The engine is transport-agnostic (mirrors how ``Broker`` takes a ``send`` callable): it is
handed an async ``emit_json`` and a sync ``emit_binary`` by the audio-WS client and never
imports websockets itself. Responsibilities (Spec 0 §5):

- **advertise** the source streams the station offers (demand-gated production).
- **source_subscribe / _unsubscribe** — start/stop an RX Opus bridge on demand; pump its
  packets as §5.3 binary media frames.
- **mic_state** — start/stop a TX bridge for the routed module; inject inbound ``op.mic``
  media.
- **tx_route** — hold which module the operator mic transmits into.

Two independent safety timers guard the mic→TX path (defence-in-depth beside the
server lock/PTT gate; the authoritative carrier un-key is the control-plane PTT dead-man in
``broker.py`` — these timers only tear down the AUDIO bridge, not the carrier):
- a **mic dead-man** that stops TX audio if no mic media arrives within ``dead_man_timeout``
  (link-loss), and
- an absolute **time-out-timer (TOT)** that stops TX audio after ``max_tx_seconds`` of
  continuous keying regardless of media (standard amateur-radio practice; also bounds a
  looping/injected uplink).

Blocking work (PipeWire ``pw-dump``/``udevadm`` subprocesses in the backend, socket bind +
gst-launch spawn in a bridge) is offloaded to a thread so it never stalls the WS event loop.
RX bridges call ``on_opus`` from their reader thread; ``emit_binary`` must therefore be a
thread-safe hand-off (the WS client schedules the actual send on its loop).
"""

from __future__ import annotations

import asyncio
import logging

from station_agent.audio import frame
from station_agent.audio.bridge_factory import BridgeFactory
from station_agent.audio.streams import OP_MIC, StreamRegistry

try:
    from station_agent.audio.diagnostics import DIAG_STREAM_REF
except ImportError:  # pragma: no cover — guard against hypothetical future cycle
    DIAG_STREAM_REF = 0xFFFE

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
        max_tx_seconds: float = 180.0,
    ):
        self._backend = backend
        self._emit_json = emit_json
        self._emit_binary = emit_binary
        self._factory = bridge_factory or BridgeFactory()
        self.registry = StreamRegistry(rx_rate=rx_rate, mic_rate=mic_rate)
        self._dead_man_timeout = dead_man_timeout
        self._max_tx_seconds = max_tx_seconds
        # stream_id -> {"bridge", "seq", "ts", "rate", "ref", "dead"}
        self._rx: dict[str, dict] = {}
        self._tx = None  # {"bridge": tx_bridge, "slot": int, "module": str}
        self._diag: dict | None = None  # {"bridge": diag_bridge, "slot": int}
        self._diag_task: asyncio.Task | None = None
        self._dead_man: asyncio.Task | None = None
        # A token identifies the *current* dead-man arming. cancel() alone cannot un-fire a
        # task whose sleep has already elapsed but not yet resumed, so the loop re-checks the
        # token and no-ops if it was superseded (e.g. by a mic frame that just re-armed).
        self._dead_man_token: object | None = None
        self._tot: asyncio.Task | None = None
        self.tx_route: dict | None = None

    @staticmethod
    async def _to_thread(fn, *args):
        """Run a blocking call off the event loop (backend subprocess / bridge start-stop)."""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Discover audio slots, (re)build the stream set, and advertise."""
        slots = await self._to_thread(self._backend.list_audio_slots)
        self.registry.rebuild(slots)
        await self.advertise()

    async def advertise(self) -> None:
        await self._emit_json(self.registry.advertise_payload())

    async def stop(self) -> None:
        for stream_id in list(self._rx):
            await self.on_source_unsubscribe(stream_id)
        await self._teardown_tx()
        await self._teardown_diag()

    # --- RX (demand-gated source production) -------------------------------
    async def on_source_subscribe(self, stream_id: str) -> None:
        info = self.registry.get(stream_id)
        if info is None:
            await self._emit_error("unknown_stream", f"no such stream {stream_id!r}")
            return
        if stream_id == OP_MIC:
            # op.mic is browser-produced; the agent never encodes it as a source. A server
            # that subscribes it is misusing the contract — log, don't ship a bogus error.
            logger.debug("engine: ignoring source_subscribe for non-producible op.mic")
            return
        if stream_id in self._rx:
            return  # already producing; demand is server-tracked
        node = await self._to_thread(self._backend.resolve_node, info.slot, "rx")
        if node is None:
            # Agent-side lifecycle failure (no §5.5 client-error code fits) → stream_state.
            await self._emit_stream_state(stream_id, "error", "RX node unresolved")
            return
        state = {"seq": 0, "ts": 0, "rate": info.rate, "ref": info.stream_ref, "dead": False}

        def on_opus(payload: bytes, _st=state, _sid=stream_id) -> None:
            self._emit_media(_sid, _st, payload)

        bridge = self._factory.make_rx(node, info.rate, on_opus)
        state["bridge"] = bridge
        self._rx[stream_id] = state
        try:
            await self._to_thread(bridge.start)
        except Exception:  # noqa: BLE001 — a failed start must release the port, not leak it
            logger.exception("engine: RX bridge start failed for %s", stream_id)
            self._rx.pop(stream_id, None)
            state["dead"] = True
            await self._to_thread(_safe_stop, bridge)
            await self._emit_stream_state(stream_id, "error", "RX bridge start failed")
            return
        await self._emit_stream_state(stream_id, "live", "")

    async def on_source_unsubscribe(self, stream_id: str) -> None:
        state = self._rx.pop(stream_id, None)
        if state is None:
            return
        # Close the in-flight-callback window: a reader-thread on_opus already past its dead
        # check may emit one last frame, but stop() joins the reader so none survive after.
        state["dead"] = True
        await self._to_thread(_safe_stop, state["bridge"])
        await self._emit_stream_state(stream_id, "idle", "")

    def _emit_media(self, stream_id: str, state: dict, payload: bytes) -> None:
        if state.get("dead"):
            return  # stream was unsubscribed; drop a late reader-thread callback
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
        if not isinstance(tx_slot, int) or isinstance(tx_slot, bool):
            logger.warning("engine: mic_state with non-int tx_slot %r — ignoring", tx_slot)
            return
        if self._diag is not None:
            # RF safety: abort an in-flight diagnostic the moment a valid PTT-active
            # mic_state arrives — unconditionally and before any slow/fallible node
            # lookups. This ensures the tone source is stopped even when resolve_node
            # returns None (which would otherwise cause an early return, skipping teardown).
            logger.info("engine: mic_state active — aborting in-flight diagnostic")
            await self._teardown_diag()
        mic_info = self.registry.get(OP_MIC)
        if mic_info is None:
            logger.warning("engine: op.mic not registered — cannot start TX")
            return
        node = await self._to_thread(self._backend.resolve_node, tx_slot, "tx")
        if node is None:
            logger.warning("engine: no TX node for slot %s — mic not injected", tx_slot)
            return
        if self._tx is not None:
            await self._teardown_tx()
        bridge = self._factory.make_tx(node, mic_info.rate)
        self._tx = {"bridge": bridge, "slot": tx_slot, "module": tx_module}
        try:
            await self._to_thread(bridge.start)
        except Exception:  # noqa: BLE001 — release the port on a failed start
            logger.exception("engine: TX bridge start failed for slot %s", tx_slot)
            self._tx = None
            await self._to_thread(_safe_stop, bridge)
            return
        self._arm_dead_man()
        self._arm_tot()

    async def on_media_frame(self, data: bytes) -> None:
        try:
            mf = frame.parse_frame(data)
        except frame.FrameError:
            logger.debug("engine: dropping malformed media frame")
            return
        # Route diagnostic reference frames (server-originated U-anchor) to the diagnostic
        # bridge BEFORE the TX gate.  Match only the per-run ref so frames from a
        # concurrent refused/stale run cannot pollute the active diagnostic bridge.
        if self._diag is not None and mf.stream_ref == self._diag["ref"]:
            self._diag["bridge"].feed_opus(mf.payload)
            return
        if self._tx is None or mf.stream_ref != self.registry.mic_ref:
            return  # only op.mic media is injected, and only while TX is up
        self._tx["bridge"].feed_opus(mf.payload)
        self._arm_dead_man()  # pet the dead-man (NOT the TOT — that is an absolute ceiling)

    async def set_tx_route(self, slot, module) -> None:
        self.tx_route = None if slot is None else {"slot": slot, "module": module}

    # --- diagnostics -------------------------------------------------------
    async def on_diag_command(self, command: dict) -> dict | None:
        """Validate and dispatch an audio-path diagnostic command.

        * Anchor **C**: runs synchronously off-thread, returns the ``diag_result``
          dict directly (same as before).
        * Anchor **U**: non-blocking — starts the diagnostic bridge + a background
          measurement task and returns ``None``; the ``diag_result`` is emitted via
          ``emit_json`` once the measurement completes.

        RF safety: refused while a TX bridge is active (keyed transmitter). A
        second U is refused while one is already running.  Never raises into the
        WS loop.
        """
        from station_agent.audio import diagnostics  # local import to keep top-level clean

        rid = command.get("request_id")
        anchor = command.get("anchor")
        slot = command.get("slot")
        signal = command.get("signal") or {}
        base = {"v": 1, "type": "diag_result", "request_id": rid}
        if anchor not in ("C", "U"):
            return {**base, "error": f"unsupported anchor {anchor!r}"}
        if not isinstance(slot, int) or isinstance(slot, bool):
            return {**base, "error": "slot must be an int"}
        # RF safety + exclusivity: refuse while a TX bridge (PTT/mic) is up.
        if self._tx is not None:
            return {
                **base,
                "busy": True,
                "error": "refused: TX active — diagnostic inject would reach a keyed transmitter",
            }
        # Mutual exclusion: only one diagnostic (any anchor) at a time.
        if self._diag is not None:
            return {
                **base,
                "busy": True,
                "error": "refused: a diagnostic is already running",
            }
        if anchor == "U":
            diag_ref = command.get("diag_ref", DIAG_STREAM_REF)
            return await self._start_u_diagnostic(base, slot, signal, diag_ref)
        # anchor C — synchronous inject/measure (unchanged behaviour)
        try:
            report = await self._to_thread(
                lambda: diagnostics.run_diagnostic(
                    anchor=anchor,
                    slot=slot,
                    signal=signal,
                    backend=self._backend,
                    rate=self.registry.mic_rate,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a diag failure must not kill the WS loop
            logger.exception("engine: diagnostic run failed")
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        return {**base, **report}

    async def _start_u_diagnostic(
        self, base: dict, slot: int, signal: dict, diag_ref: int
    ) -> dict | None:
        """Start a non-blocking anchor-U diagnostic run.

        Returns ``None`` on acceptance (result emitted later); returns a busy/error
        dict if the run is refused or the bridge fails to start.

        *diag_ref* is the per-run stream_ref assigned by the orchestrator (≥ 0x8000)
        so that frames can only reach the run they belong to.  Falls back to the
        global ``DIAG_STREAM_REF`` sentinel when the command omits the field.
        """
        if self._diag is not None:
            return {**base, "busy": True, "error": "refused: a diagnostic is already running"}
        node = await self._to_thread(self._backend.resolve_node, slot, "tx")
        if node is None:
            return {**base, "error": f"no TX node for slot {slot}"}
        bridge = self._factory.make_diag_u(node, self.registry.mic_rate)
        try:
            await self._to_thread(bridge.start)
        except Exception as exc:  # noqa: BLE001 — a failed start must release the port
            logger.exception("engine: U diagnostic bridge start failed")
            await self._to_thread(_safe_stop, bridge)
            return {**base, "error": f"U diagnostic bridge start failed: {exc}"}
        self._diag = {"bridge": bridge, "slot": slot, "ref": diag_ref}
        self._diag_task = asyncio.ensure_future(
            self._run_u_measurement(base, slot, signal, bridge)
        )
        return None  # diag_result emitted asynchronously once measurement completes

    async def _run_u_measurement(self, base: dict, slot: int, signal: dict, bridge) -> None:
        """Background task: read PCM from the diagnostic bridge and emit the result."""
        from station_agent.audio import diagnostics

        try:
            report = await self._to_thread(
                lambda: diagnostics.finish_u_diagnostic(
                    bridge,
                    backend=self._backend,
                    slot=slot,
                    signal=signal,
                    rate=self.registry.mic_rate,
                )
            )
            result = {**base, **report}
        except Exception as exc:  # noqa: BLE001 — never let a diag failure escape
            logger.exception("engine: U diagnostic measurement failed")
            result = {**base, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            await self._teardown_diag()
        await self._emit_json(result)

    async def _teardown_diag(self) -> None:
        """Tear down the in-flight diagnostic bridge and cancel the measurement task.

        Safe to call from both ``stop()`` and from within ``_run_u_measurement``'s
        ``finally`` block: when called from the measurement task itself we must NOT
        cancel the current task (that would inject ``CancelledError`` at the
        ``_safe_stop`` await and skip bridge cleanup), hence the
        ``task is not asyncio.current_task()`` guard.
        """
        task = self._diag_task
        self._diag_task = None
        diag = self._diag
        self._diag = None
        if diag is not None:
            await self._to_thread(_safe_stop, diag["bridge"])
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    # --- safety timers -----------------------------------------------------
    def _arm_dead_man(self) -> None:
        self._disarm_dead_man()
        token = object()
        self._dead_man_token = token
        self._dead_man = asyncio.ensure_future(self._dead_man_loop(token))

    def _disarm_dead_man(self) -> None:
        self._dead_man_token = None
        if self._dead_man is not None:
            self._dead_man.cancel()
            self._dead_man = None

    async def _dead_man_loop(self, token: object) -> None:
        await asyncio.sleep(self._dead_man_timeout)
        if self._dead_man_token is not token:
            return  # superseded by a re-arm after the sleep elapsed; cancel could not un-fire
        # Detach self BEFORE teardown so _disarm_dead_man does not cancel this running task
        # (which would inject CancelledError at the offloaded _safe_stop await and skip it).
        self._dead_man = None
        self._dead_man_token = None
        logger.info("engine: mic dead-man fired — tearing down TX audio")
        await self._teardown_tx()

    def _arm_tot(self) -> None:
        self._disarm_tot()
        self._tot = asyncio.ensure_future(self._tot_loop())

    def _disarm_tot(self) -> None:
        if self._tot is not None:
            self._tot.cancel()
            self._tot = None

    async def _tot_loop(self) -> None:
        await asyncio.sleep(self._max_tx_seconds)
        # Detach self before teardown (see _dead_man_loop) so _disarm_tot does not self-cancel.
        self._tot = None
        logger.warning(
            "engine: mic TOT fired after %.0fs — force-stopping TX audio", self._max_tx_seconds
        )
        await self._teardown_tx()

    async def _teardown_tx(self) -> None:
        self._disarm_dead_man()
        self._disarm_tot()
        if self._tx is not None:
            bridge = self._tx["bridge"]
            self._tx = None
            await self._to_thread(_safe_stop, bridge)

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
