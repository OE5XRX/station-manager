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
import time

from station_agent.audio import frame
from station_agent.audio.bridge_factory import BridgeFactory
from station_agent.audio.streams import OP_MIC, StreamRegistry

try:
    from station_agent.audio.diagnostics import DIAG_STREAM_REF
except ImportError:  # pragma: no cover — guard against hypothetical future cycle
    DIAG_STREAM_REF = 0xFFFE

logger = logging.getLogger(__name__)

# A hot-plug refresh restarts a dead RX pipeline at most this many times in a row before it
# reports the stream as errored (bounds respawn churn of a persistently failing pipeline).
_MAX_DEAD_RESTARTS = 3

# After a hot-plug re-advertise moves op.mic's stream_ref, mic media still carrying the old
# ref is accepted this long — until the server/browser remapped — so an unrelated keyed TX
# is not dropped into the mic dead-man by a slot change elsewhere.
_MIC_REF_GRACE_S = 5.0

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
        # Identifies the current TX bridge's meter stream; readings carrying a stale token
        # (torn-down/superseded bridge) are dropped.
        self._tx_meter_token: object | None = None
        self._meter_pending: tuple[object, dict] | None = None
        self._meter_task: asyncio.Task | None = None  # strong ref to the in-flight send
        self._diag: dict | None = None  # {"bridge": diag_bridge, "slot": int}
        self._diag_task: asyncio.Task | None = None
        self._dead_man: asyncio.Task | None = None
        # A token identifies the *current* dead-man arming. cancel() alone cannot un-fire a
        # task whose sleep has already elapsed but not yet resumed, so the loop re-checks the
        # token and no-ops if it was superseded (e.g. by a mic frame that just re-armed).
        self._dead_man_token: object | None = None
        self._tot: asyncio.Task | None = None
        self.tx_route: dict | None = None
        # Tracks whether the server has PTT active, independently of whether the audio TX
        # bridge is up.  Set to True as soon as mic_state(active=True) is received — even
        # if the bridge fails to start — so the RF interlock in on_diag_command can refuse
        # diagnostic injection regardless of _tx state.  Cleared only when the server sends
        # mic_state(active=False); safety timers (dead-man, TOT) do NOT clear this because
        # they only tear down the audio bridge, not the carrier/PTT.
        self._mic_active: bool = False
        # Serialises clear-refs + _safe_stop inside _teardown_diag so the
        # "cleared but bridge not yet stopped" window is never observable to
        # a concurrent on_mic_state / stop that also acquires this lock.
        self._diag_lock: asyncio.Lock = asyncio.Lock()
        # Serialises RX stream start/stop against a hot-plug refresh: the server re-sends
        # source_subscribe for every demanded stream on each advertise, which must not race
        # the refresh's own restart of the same stream into a second bridge.
        self._rx_lock: asyncio.Lock = asyncio.Lock()
        # Set by stop(): a refresh/subscribe scheduled cross-thread may still run after the
        # connection dropped and must not start bridges on this discarded engine.
        self._closed = False
        # stream_id -> consecutive dead-bridge restarts by refresh; bounds the respawn churn
        # of a pipeline that keeps dying (reset once a refresh finds it alive again).
        self._rx_restarts: dict[str, int] = {}
        # RX streams the server currently demands (subscribe minus unsubscribe). A stream whose
        # (re)start failed drops out of _rx but stays demanded, so a later refresh retries it —
        # the server re-sends source_subscribe only on the first-demand edge or an advertise.
        self._rx_demand: set[str] = set()
        # Serialises a TX bridge start (on_mic_state) against a hot-plug refresh's TX
        # teardown, so the teardown never stops a bridge whose start is still in flight.
        self._tx_lock: asyncio.Lock = asyncio.Lock()
        # (old op.mic ref, monotonic deadline) after a ref-moving refresh; see _MIC_REF_GRACE_S.
        self._prev_mic_ref: tuple[int, float] | None = None

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

    async def refresh(self, slots, rebuild=frozenset()) -> None:
        """Apply a hot-plug change of the audio slot set (module moved, plugged, unplugged).

        ``rebuild`` names slots whose audio device re-enumerated in place (e.g. after a module
        firmware DFU): the stream set is unchanged but the old PipeWire node is gone, so their
        pipelines are rebuilt. Streams of vanished slots stop with a clear ``idle`` state;
        surviving streams whose slot re-enumerated, whose ``stream_ref`` moved, or whose bridge
        died are restarted — after the re-advertise, so no frame carries a ref the server has
        not mapped yet. A TX/diagnostic bridge on a vanished or re-enumerated slot is torn
        down; the server's next ``mic_state`` keepalive restarts TX on the new node.
        Re-advertises only if the stream set changed; otherwise only dead/rebuilt pipelines
        are touched, so an unchanged periodic refresh is a no-op.
        """
        rebuild = set(rebuild)
        gone: list[str] = []
        restart: list[str] = []
        given_up: list[str] = []
        async with self._rx_lock:
            if self._closed:
                return
            before = self.registry.advertise_payload()
            old_mic_ref = self.registry.mic_ref
            self.registry.rebuild(slots)
            if old_mic_ref is not None and old_mic_ref != self.registry.mic_ref:
                self._prev_mic_ref = (old_mic_ref, time.monotonic() + _MIC_REF_GRACE_S)
            changed = self.registry.advertise_payload() != before
            for stream_id, state in list(self._rx.items()):
                info = self.registry.get(stream_id)
                alive = getattr(state["bridge"], "is_alive", lambda: True)()
                if info is None:
                    gone.append(stream_id)
                elif info.slot in rebuild or info.stream_ref != state["ref"]:
                    restart.append(stream_id)
                elif not alive:
                    n = self._rx_restarts.get(stream_id, 0) + 1
                    self._rx_restarts[stream_id] = n
                    (restart if n <= _MAX_DEAD_RESTARTS else given_up).append(stream_id)
                else:
                    self._rx_restarts.pop(stream_id, None)
            for stream_id in gone + restart + given_up:
                await self._stop_rx(stream_id)
            if changed:
                logger.info("engine: audio slots now %s; re-advertising", sorted(slots))
                await self.advertise()
            # Restart inside the lock (after the advertise, so no frame carries an unmapped
            # ref): a source_unsubscribe arriving meanwhile must see the restarted stream, or
            # it would no-op and leave an undemanded pipeline running.
            for stream_id in restart:
                logger.info("engine: rebuilding RX pipeline for %s", stream_id)
                await self._start_rx(stream_id)
            # Demanded streams whose earlier (re)start failed (e.g. the PipeWire node lagged
            # the sysfs card): retry, bounded by the same consecutive-restart counter.
            for stream_id in sorted(self._rx_demand - set(self._rx) - set(given_up)):
                if self.registry.get(stream_id) is None:
                    continue
                n = self._rx_restarts.get(stream_id, 0) + 1
                self._rx_restarts[stream_id] = n
                if n > _MAX_DEAD_RESTARTS:
                    given_up.append(stream_id)
                    continue
                logger.info("engine: retrying RX pipeline for %s", stream_id)
                await self._start_rx(stream_id)
        for stream_id in gone:
            self._rx_restarts.pop(stream_id, None)
            self._rx_demand.discard(stream_id)
            await self._emit_stream_state(stream_id, "idle", "audio slot removed")
        for stream_id in given_up:
            self._rx_restarts.pop(stream_id, None)
            self._rx_demand.discard(stream_id)
            logger.warning("engine: RX pipeline for %s keeps dying; giving up", stream_id)
            await self._emit_stream_state(stream_id, "error", "RX pipeline keeps failing")

        def _affected(slot) -> bool:
            return slot not in slots or slot in rebuild

        async with self._tx_lock:
            if self._tx is not None and _affected(self._tx["slot"]):
                logger.info(
                    "engine: TX slot %s vanished/re-enumerated; tearing down", self._tx["slot"]
                )
                await self._teardown_tx()
        diag = self._diag
        if diag is not None and _affected(diag["slot"]):
            logger.info("engine: diagnostic slot vanished/re-enumerated; aborting")
            torn = await self._teardown_diag()
            # The cancelled measurement never emits its result — answer the command here so
            # the server does not wait for its own timeout. Only if this teardown actually
            # aborted that run (it may have finished and answered concurrently).
            if torn is diag and diag.get("base") is not None:
                await self._emit_json(
                    {**diag["base"], "error": "aborted: audio slot vanished or re-enumerated"}
                )

    async def stop(self) -> None:
        # Closed before taking the lock: a refresh/subscribe queued behind it then bails
        # instead of starting a bridge after the snapshot below.
        self._closed = True
        async with self._rx_lock:
            stopped = list(self._rx)
            for stream_id in stopped:
                await self._stop_rx(stream_id)
        for stream_id in stopped:
            await self._emit_stream_state(stream_id, "idle", "")
        await self._teardown_tx()
        await self._teardown_diag()

    def slot_active(self, slot: int) -> bool:
        """True while *slot* carries the TX/mic route or a diagnostic run.

        Called cross-thread by the module_ota cycle (a busy slot is not flashed). RX-only
        streams deliberately do NOT count: any viewer subscribes RX on page load without a
        control lock, so counting RX would let one forgotten browser tab postpone a flash
        indefinitely while the drain refuses every control grant. RX simply drops for the
        duration of the flash (the module card shows "Firmware-Update läuft").
        Each read is a single attribute load on a snapshot reference; the answer is advisory.
        """
        tx, diag = self._tx, self._diag
        return (tx is not None and tx.get("slot") == slot) or (
            diag is not None and diag.get("slot") == slot
        )

    # --- RX (demand-gated source production) -------------------------------
    async def on_source_subscribe(self, stream_id: str) -> None:
        async with self._rx_lock:
            if stream_id != OP_MIC and self.registry.get(stream_id) is not None:
                self._rx_demand.add(stream_id)
            await self._start_rx(stream_id)

    async def _start_rx(self, stream_id: str) -> None:
        if self._closed:
            return
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
        async with self._rx_lock:
            self._rx_demand.discard(stream_id)
            self._rx_restarts.pop(stream_id, None)
            stopped = await self._stop_rx(stream_id)
        if stopped:
            await self._emit_stream_state(stream_id, "idle", "")

    async def _stop_rx(self, stream_id: str) -> bool:
        """Stop *stream_id*'s RX bridge (caller holds ``_rx_lock``); False if not running."""
        state = self._rx.pop(stream_id, None)
        if state is None:
            return False
        # Close the in-flight-callback window: a reader-thread on_opus already past its dead
        # check may emit one last frame, but stop() joins the reader so none survive after.
        state["dead"] = True
        await self._to_thread(_safe_stop, state["bridge"])
        return True

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
            self._mic_active = False
            async with self._tx_lock:  # after an in-flight start, never beside it
                await self._teardown_tx()
            return
        if not isinstance(tx_slot, int) or isinstance(tx_slot, bool):
            logger.warning("engine: mic_state with non-int tx_slot %r — ignoring", tx_slot)
            return
        # Mark PTT active BEFORE any lookups or early returns so that every code path in
        # the active=True branch — including failures in resolve_node or bridge.start() —
        # leaves _mic_active=True.  The diagnostic RF interlock checks this flag, not _tx,
        # so that a failed bridge start cannot open a window for diagnostic injection while
        # PTT is asserted.
        self._mic_active = True
        # RF safety: always pass through _teardown_diag (lock-guarded) before starting
        # TX so any in-flight diagnostic bridge is fully stopped before the TX bridge
        # starts — even if the bridge stop is slow (up to 3 s _safe_stop).  This closes
        # the overlap window where _diag was already None but the bridge was still running.
        if self._diag is not None:
            logger.info("engine: mic_state active — aborting in-flight diagnostic")
        await self._teardown_diag()
        async with self._tx_lock:
            await self._start_tx(tx_slot, tx_module)

    async def _start_tx(self, tx_slot: int, tx_module) -> None:
        # Idempotent re-assert: the server re-sends mic_state(active) on every PTT
        # keepalive (~1/s). Restarting the bridge for the same route would drop ~a few
        # hundred ms of audio each second (chopped syllables) and flicker the TX meter.
        # Keep the running bridge; do NOT re-arm the TOT (it bounds
        # this bridge run; a keepalive after a TOT/dead-man teardown starts a fresh run — the
        # server-side PTT policy owns any overall limit) and do NOT pet the dead-man
        # (that tracks media, not keepalives). A crashed bridge (is_alive() False) is NOT kept:
        # the keepalive restarts it, as before idempotence.
        if (
            self._tx is not None
            and self._tx["slot"] == tx_slot
            and self._tx["module"] == tx_module
            and getattr(self._tx["bridge"], "is_alive", lambda: True)()
        ):
            return
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
        loop = asyncio.get_running_loop()
        token = object()
        self._tx_meter_token = token

        def on_meter(reading: dict, _token=token, _slot=tx_slot) -> None:
            # Reader thread -> loop. The token drops readings from a superseded/torn-down
            # bridge (the reader may emit one last chunk while stop() joins it).
            try:
                loop.call_soon_threadsafe(self._emit_tx_meter, _token, _slot, reading)
            except RuntimeError:
                return  # loop closed (shutdown): don't spam tracebacks at the meter rate

        bridge = self._factory.make_tx(node, mic_info.rate, on_meter=on_meter)
        self._tx = {"bridge": bridge, "slot": tx_slot, "module": tx_module}
        try:
            await self._to_thread(bridge.start)
        except Exception:  # noqa: BLE001 — release the port on a failed start
            logger.exception("engine: TX bridge start failed for slot %s", tx_slot)
            self._tx = None
            self._tx_meter_token = None
            await self._to_thread(_safe_stop, bridge)
            return
        # Surface the DSP mode even when no meter reader runs (e.g. "failed"). Digital
        # metering only — never touches PTT/SA818.
        self._emit_tx_meter(
            token,
            tx_slot,
            {
                "peak_dbfs": None,
                "rms_dbfs": None,
                "gain_reduction_db": 0.0,
                "limiting": False,
                "dsp": getattr(bridge, "dsp_mode", "off"),
                "ceiling_dbfs": getattr(bridge, "ceiling_dbfs", None),
            },
        )
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
        if self._tx is None or not self._is_mic_ref(mf.stream_ref):
            return  # only op.mic media is injected, and only while TX is up
        self._tx["bridge"].feed_opus(mf.payload)
        self._arm_dead_man()  # pet the dead-man (NOT the TOT — that is an absolute ceiling)

    def _is_mic_ref(self, ref: int) -> bool:
        if ref == self.registry.mic_ref:
            return True
        prev = self._prev_mic_ref
        if prev is None:
            return False
        if time.monotonic() >= prev[1]:
            self._prev_mic_ref = None
            return False
        return ref == prev[0]

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
        # RF safety + exclusivity: refuse while PTT is active OR a TX bridge is up.
        # _mic_active is set as soon as mic_state(active=True) arrives — even when the audio
        # bridge fails to start (_tx stays None).  Interlocking on _tx alone would miss the
        # window where PTT is asserted but the bridge never came up, allowing a diagnostic to
        # inject a tone into a keyed transmitter.
        if self._tx is not None or self._mic_active:
            return {
                **base,
                "busy": True,
                "error": "refused: TX active (PTT keyed) — tone inject would RF-emit",
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
        self._diag = {"bridge": bridge, "slot": slot, "ref": diag_ref, "base": base}
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

    async def _teardown_diag(self) -> dict | None:
        """Tear down the in-flight diagnostic bridge and cancel the measurement task.

        Returns the diagnostic it cleared (``None`` if none was running).

        Safe to call from both ``stop()`` and from within ``_run_u_measurement``'s
        ``finally`` block: when called from the measurement task itself we must NOT
        cancel the current task (that would inject ``CancelledError`` at the
        ``_safe_stop`` await and skip bridge cleanup), hence the
        ``task is not asyncio.current_task()`` guard.

        The ``_diag_lock`` is held for the full clear-refs + ``_safe_stop`` sequence
        so the "diag cleared but bridge not yet stopped" state is never visible to a
        concurrent ``on_mic_state`` or ``stop()`` that also acquires the lock.  This
        closes the TX-start overlap window (Finding B): a PTT key arriving during
        teardown waits here until the diagnostic bridge is fully stopped before the
        TX bridge starts.
        """
        async with self._diag_lock:
            task = self._diag_task
            self._diag_task = None
            diag = self._diag
            self._diag = None
            if diag is not None:
                await self._to_thread(_safe_stop, diag["bridge"])
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            return diag

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
            slot = self._tx["slot"]
            self._tx = None
            # Invalidate BEFORE stopping so a last reader chunk is dropped.
            self._tx_meter_token = None
            await self._to_thread(_safe_stop, bridge)
            await self._emit_json({"v": 1, "type": "tx_meter", "slot": slot, "active": False})

    # --- helpers -----------------------------------------------------------
    def _emit_tx_meter(self, token, slot: int, reading: dict) -> None:
        if token is not self._tx_meter_token:
            return
        msg = {"v": 1, "type": "tx_meter", "slot": slot, "active": True, **reading}
        # Latest-wins coalescing: at most one send in flight; while it is pending only the
        # newest reading is kept (a slow WS must not queue an unbounded task backlog).
        self._meter_pending = (token, msg)
        if self._meter_task is None:
            self._meter_task = asyncio.ensure_future(self._drain_tx_meter())

    async def _drain_tx_meter(self) -> None:
        try:
            while self._meter_pending is not None:
                token, msg = self._meter_pending
                self._meter_pending = None
                if token is not self._tx_meter_token:
                    continue  # superseded/torn down while waiting
                await self._emit_json(msg)
        finally:
            self._meter_task = None

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
