"""Persistent outbound Control-WebSocket for the Station Agent.

Reuses the proven Ed25519 outbound-WS pattern from terminal.py (signed timestamp +
body-hash query params, exponential reconnect backoff), but is PERSISTENT while the
station is online (design spec §9). It runs the device-agnostic Broker: on connect it
discovers modules, pushes an ``inventory`` snapshot, then relays server ``command`` /
``subscribe`` / ``unsubscribe`` / ``ptt_keepalive`` into the broker and the broker's
``inventory`` / ``state`` / ``result`` / ``event`` back up. A disconnect fires the PTT
dead-man locally.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlencode

import websockets

from . import slot_pause
from .broker import Broker
from .config import AgentConfig
from .protocol import ProtocolError, build_event, parse_message
from .signing import load_private_key
from .slot_discovery import discover_slots

logger = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0
# Cross-thread slot_busy() budget: a wedged control loop must not stall the module_ota cycle.
SLOT_BUSY_TIMEOUT_S = 2.0


def _log_send_failure(fut, event: str, slot) -> None:
    if fut.cancelled():
        logger.warning("Control: event %s for slot %s cancelled before send", event, slot)
    elif fut.exception() is not None:
        logger.warning(
            "Control: event %s for slot %s failed to send: %s", event, slot, fut.exception()
        )


class _SlotInventoryDebouncer:
    """Hysteresis over successive ``discover_slots`` results so a *transient* probe miss
    does not drop a previously-present slot from inventory.

    ``discover_slots`` omits a slot both when the module is genuinely gone and when a
    single probe fails (corrupted MODULE-LIST, chatty console, one-off timeout). Treating
    those alike flaps the module online/offline every re-scan. A slot that was present but
    is missing this cycle is retained (last-known-good) for up to ``max_misses`` consecutive
    misses, then dropped — genuine removal still surfaces after a bounded delay, a transient
    miss never flaps. ``max_misses=0`` restores legacy drop-on-first-miss. ``update`` is
    pure given its call history; the effective inventory is returned sorted by slot so the
    caller's change-detection is deterministic.

    A slot paused for a module firmware update (``slot_pause``) is skipped by the scan, so it
    is absent from ``discovered`` by design: it never counts a miss (its counter is reset, so
    after resume the re-enumerating module gets the full ``max_misses`` budget), and it is
    reported as its ``updating`` entry. The retained last-known-good entry stays the raw one.
    A paused slot WITHOUT a snapshot is still reported here (updating view derived from
    last-known-good), whereas the heartbeat inventory — which has no history — omits it.
    """

    def __init__(self, max_misses: int = 2):
        self._max = max_misses
        self._last_good: dict = {}
        self._miss: dict = {}

    def update(self, discovered: list) -> list:
        paused = slot_pause.paused_slots()
        present = {e.get("slot"): e for e in discovered if e.get("slot") not in paused}
        for slot, entry in present.items():
            self._last_good[slot] = entry
            self._miss[slot] = 0
        for slot in list(self._last_good):
            if slot in present:
                continue
            if slot in paused:
                self._miss[slot] = 0
                continue
            self._miss[slot] = self._miss.get(slot, 0) + 1
            if self._miss[slot] > self._max:
                del self._last_good[slot]
                del self._miss[slot]
        effective = dict(self._last_good)
        for slot in paused:
            entry = slot_pause.updating_entry(slot)
            if entry is None and slot in self._last_good:
                # Paused without a snapshot: derive the updating view from last-known-good.
                entry = copy.deepcopy(self._last_good[slot])
                for module in entry.get("modules", []):
                    module["status"] = "updating"
            if entry is not None:
                effective[slot] = entry
        return [effective[slot] for slot in sorted(effective)]

    def last_good(self, slot: int) -> dict | None:
        return self._last_good.get(slot)

    def forget(self, slot: int) -> None:
        """Drop *slot*'s last-known-good entry (and miss counter) at once.

        Used after a flash ends with the module hung in DFU mode: it has no CDC, so the
        scan cannot see it, and re-emitting the stale pre-flash identity would tell the
        server the module is back online (clearing its DFU-stuck notice). The next
        ``update`` then omits the slot unless the scan genuinely finds it again."""
        self._last_good.pop(slot, None)
        self._miss.pop(slot, None)

    def snapshot(self) -> dict:
        """Deep copy of the retained last-known-good entries, keyed by slot."""
        return copy.deepcopy(self._last_good)


class ControlClient:
    def __init__(self, config: AgentConfig, *, virtual_modules=None, on_firmware_check=None):
        self._config = config
        # Server `firmware_check` nudge, forwarded to each connection's Broker.
        self._on_firmware_check = on_firmware_check
        # Per-connection state, set in _connect_and_serve and cleared on disconnect. Read
        # cross-thread by the module_ota hooks (slot_busy / send_event / refresh) — always
        # through a local snapshot of the reference.
        self._broker: Broker | None = None
        self._refresh_event: asyncio.Event | None = None
        # Latest debounced last-known-good entry per slot: the slot_pause snapshot source.
        self._inv_lock = threading.Lock()
        self._last_entries: dict = {}
        # Slots whose debounced last-known-good must be dropped before the next debounce
        # update (forget_slot, cross-thread; applied on the control loop). Guarded by
        # _inv_lock.
        self._forget_pending: set = set()
        # Set by request_inventory_refresh(force=True) (cross-thread; guarded by
        # _inv_lock): the next re-discovery cycle emits even if the inventory is unchanged.
        self._force_emit = False
        # Virtual control-plane modules (e.g. the audio-router, Spec 0 §5.6) forwarded to the
        # Broker on connect. None ⇒ physical-only behaviour, unchanged.
        self._virtual_modules = virtual_modules
        self._ws = None
        self._shutdown = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._private_key = load_private_key(config.ed25519_key_path)
        if self._private_key is None:
            raise RuntimeError(
                "Control: Ed25519 key could not be loaded; WebSocket authentication is impossible"
            )

    def _build_ws_url(self) -> str:
        server = self._config.server_url
        if server.startswith("https://"):
            ws_base = "wss://" + server[len("https://") :]
        elif server.startswith("http://"):
            ws_base = "ws://" + server[len("http://") :]
        else:
            ws_base = "wss://" + server
        path = f"/ws/agent/control/{self._config.station_id}/"
        timestamp = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        signature = self._private_key.sign(f"{timestamp}:{body_hash}".encode())
        query = {
            "station_id": str(self._config.station_id),
            "signature": base64.b64encode(signature).decode("ascii"),
            "timestamp": timestamp,
        }
        return f"{ws_base}{path}?{urlencode(query)}"

    async def _ws_send(self, msg: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(msg))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Control: _ws_send: connection already closed, dropping message")
            self._ws = None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Control: _ws_send: send failed (%s: %s), dropping message",
                type(exc).__name__,
                exc,
            )

    async def _connect_and_serve(self) -> None:
        url = self._build_ws_url()
        logger.info("Control: connecting to server")
        async with websockets.connect(
            url, ping_interval=30, ping_timeout=10, close_timeout=5
        ) as ws:
            self._ws = ws
            broker = Broker(
                self._ws_send,
                slot_command_timeout=getattr(self._config, "slot_command_timeout", 5.0),
                dead_man_timeout=getattr(self._config, "control_dead_man_timeout", 1.5),
                telemetry_default_interval_ms=getattr(
                    self._config, "telemetry_default_interval_ms", 1000
                ),
                telemetry_min_floor_ms=getattr(self._config, "telemetry_min_floor_ms", 200),
                trace_serial=getattr(self._config, "trace_serial", False),
                virtual_modules=self._virtual_modules,
                on_firmware_check=self._on_firmware_check,
            )
            self._refresh_event = asyncio.Event()
            self._broker = broker
            loop = asyncio.get_running_loop()
            if getattr(self._config, "slot_discovery_enabled", True):
                try:
                    trace = getattr(self._config, "trace_serial", False)
                    discovered = await loop.run_in_executor(
                        None,
                        lambda: discover_slots(
                            self._config.slot_dev_base,
                            trace=trace,
                            # Live view: a slot paused mid-scan is skipped from then on.
                            skip=slot_pause.LIVE_PAUSED,
                        ),
                    )
                except Exception:  # noqa: BLE001 — discovery must not break the control link
                    logger.exception("Control: slot discovery failed; reporting empty inventory")
                    discovered = []
            else:
                discovered = []
            # Debounce slot disappearance so a transient probe miss never flaps the module
            # out of inventory (see _SlotInventoryDebouncer). Seed it with the first scan so
            # the re-discovery loop starts from the same effective baseline.
            debouncer = _SlotInventoryDebouncer(
                getattr(self._config, "control_rediscovery_max_misses", 2)
            )
            self._apply_forgets(debouncer)
            effective = debouncer.update(discovered)
            self._publish_last_good(debouncer)
            broker.set_inventory(effective)
            await broker.emit_inventory()
            logger.info("Control: connected, inventory sent")

            rediscovery = loop.create_task(
                self._rediscovery_loop(broker, loop, debouncer, effective)
            )
            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    try:
                        parsed = parse_message(message)
                    except ProtocolError as exc:
                        logger.warning("Control: dropping malformed message: %s", exc)
                        continue
                    await broker.handle(parsed)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Control: WebSocket closed: %s", exc)
            finally:
                rediscovery.cancel()
                try:
                    await rediscovery
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — a re-discovery error must never skip cleanup
                    logger.exception("Control: re-discovery task errored during shutdown")
                self._refresh_event = None
                # Cleared only after on_disconnect's fail-safe unkey, so slot_busy() keeps
                # answering from the live broker until PTT is really down.
                await broker.on_disconnect()
                self._broker = None
                self._ws = None

    def _apply_forgets(self, debouncer) -> None:
        with self._inv_lock:
            pending, self._forget_pending = self._forget_pending, set()
        for slot in pending:
            debouncer.forget(slot)

    def _publish_last_good(self, debouncer) -> None:
        snapshot = debouncer.snapshot()
        with self._inv_lock:
            self._last_entries = snapshot

    async def _rediscovery_loop(self, broker, loop, debouncer, last_emitted) -> None:
        """Periodically re-scan slots; if the (debounced) inventory changed, re-emit it.

        A single startup race can yield an empty inventory (the FM console interleaves async
        logs into the MODULE-LIST reply). Discovery must not be a one-shot: re-scan on an
        interval and push a fresh inventory whenever it differs from what we last sent, so a
        module that lost the race — or is hot-plugged — comes online without a reconnect.
        Each probe is serialized against polls/commands on the same control serial by the
        process-wide per-device lock (``devlock``, held by every opener), and the scan's
        result is debounced, so neither a concurrent tick nor a one-off probe miss can flap a
        present module back offline.

        Besides the periodic interval, :meth:`request_inventory_refresh` (cross-thread) wakes
        the loop for one immediate cycle — used when a slot is paused/resumed for a module
        firmware update so the server sees ``status: updating`` without waiting up to an
        interval. ``interval <= 0`` disables only the periodic re-scan, not explicit refreshes.
        """
        interval = getattr(self._config, "control_rediscovery_interval", 30.0)
        refresh = self._refresh_event or asyncio.Event()
        enabled = getattr(self._config, "slot_discovery_enabled", True)
        trace = getattr(self._config, "trace_serial", False)
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(refresh.wait(), interval if interval > 0 else None)
            except TimeoutError:
                pass
            # Clear before scanning so a refresh requested mid-scan triggers another cycle.
            refresh.clear()
            # Consumed before the scan (like the event) so a forced refresh requested
            # mid-scan forces the following cycle, whose scan sees the resumed slot.
            with self._inv_lock:
                force, self._force_emit = self._force_emit, False
            if self._shutdown.is_set() or self._ws is None:
                return
            if not enabled:
                continue
            try:
                # The probe shares the single control serial with telemetry polls/commands;
                # a concurrent access would corrupt the MODULE-LIST reply so the slot drops
                # out of inventory and the module flaps offline every re-scan. Per-device
                # serialization is enforced by the devlock each opener holds; rediscover()
                # additionally keeps the scan cancellation-safe (shield + drain on disconnect).
                # Paused slots (module firmware update) are skipped without being opened; the
                # debouncer reports them as their `updating` entry. LIVE_PAUSED is checked per
                # slot, so a slot paused mid-scan is not probed during its flash window.
                discovered = await broker.rediscover(
                    lambda: discover_slots(
                        self._config.slot_dev_base, trace=trace, skip=slot_pause.LIVE_PAUSED
                    )
                )
            except Exception:  # noqa: BLE001 — re-discovery must never break the control link
                logger.exception("Control: re-discovery failed; keeping current inventory")
                if force:
                    with self._inv_lock:  # keep the forced emit for the next cycle
                        self._force_emit = True
                continue
            self._apply_forgets(debouncer)
            effective = debouncer.update(discovered)
            self._publish_last_good(debouncer)
            if effective == last_emitted and not force:
                continue
            if effective == last_emitted:
                logger.info("Control: forced inventory re-emit (module firmware cycle ended)")
            else:
                logger.info("Control: inventory changed on re-discovery; re-emitting")
            last_emitted = effective
            broker.set_inventory(effective)
            await broker.emit_inventory()

    # --- cross-thread hooks (module_ota cycle, main thread) ---------------------
    def last_inventory_entry(self, slot: int) -> dict | None:
        """Latest debounced last-known-good inventory entry for *slot* (a copy), or None."""
        with self._inv_lock:
            entry = self._last_entries.get(slot)
            return copy.deepcopy(entry) if entry is not None else None

    def forget_slot(self, slot: int) -> None:
        """Thread-safe: drop *slot*'s debounced last-known-good entry.

        Applied by the control loop before its next debounce update, so the following
        inventory emit omits the slot instead of re-sending the stale identity (see
        ``_SlotInventoryDebouncer.forget``). Pair with :meth:`request_inventory_refresh`."""
        with self._inv_lock:
            self._forget_pending.add(slot)

    def slot_busy(self, slot: int, timeout: float = SLOT_BUSY_TIMEOUT_S) -> bool:
        """Thread-safe: True while *slot* is keyed (PTT) on the control plane.

        Not connected (no broker / loop) ⇒ no PTT is possible ⇒ False. If the control loop
        does not answer within *timeout* — or the query fails in any other way — the answer
        is unknown ⇒ True (fail closed: busy).
        """
        broker, loop = self._broker, self._loop
        if broker is None or loop is None or not loop.is_running():
            return False

        async def _query() -> bool:
            return broker.slot_busy(slot)

        coro = _query()
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # loop closed between the check and the schedule: disconnected
            coro.close()
            return False
        try:
            return bool(fut.result(timeout))
        except TimeoutError:
            fut.cancel()
            logger.warning("Control: slot_busy(%s) timed out; treating slot as busy", slot)
            return True
        except Exception:  # noqa: BLE001 — unknown answer: fail closed
            logger.exception("Control: slot_busy(%s) failed; treating slot as busy", slot)
            return True

    def send_event(self, slot, module, event: str, detail) -> None:
        """Thread-safe fire-and-forget ``event`` frame; a no-op while disconnected."""
        loop = self._loop
        if self._ws is None or loop is None or not loop.is_running():
            return
        coro = self._ws_send(build_event(slot, module, event, detail))
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # loop closed meanwhile
            coro.close()
            logger.warning("Control: event %s for slot %s dropped (loop closed)", event, slot)
            return
        fut.add_done_callback(lambda f: _log_send_failure(f, event, slot))

    def request_inventory_refresh(self, force: bool = False) -> None:
        """Thread-safe: make the re-discovery loop run one immediate cycle (emit if changed).

        ``force`` emits even if the effective inventory is unchanged — used at the end of a
        module firmware cycle: the server soft-offlines a flashed module on
        ``firmware_update_finished`` and needs a fresh inventory frame to see it back, but a
        fast cycle can pause and resume a slot between two scans, so the debounced inventory
        never changed (no ``updating`` emit, nothing to re-emit after resume)."""
        loop, refresh = self._loop, self._refresh_event
        if loop is None or refresh is None:
            return
        if force:
            with self._inv_lock:
                self._force_emit = True
        try:
            loop.call_soon_threadsafe(refresh.set)
        except RuntimeError:  # loop closed meanwhile
            pass

    async def _run_async(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                backoff = BACKOFF_INITIAL
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("Control: connection error (%s), retrying in %.0fs", exc, backoff)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Control: unexpected error (%s: %s), retrying in %.0fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
            if self._shutdown.is_set():
                break
            wait_end = time.monotonic() + backoff
            while time.monotonic() < wait_end and not self._shutdown.is_set():
                await asyncio.sleep(0.5)
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        logger.info("Control: client stopped")

    def run(self) -> None:
        logger.info("Control: starting client")
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:  # noqa: BLE001
            logger.error("Control: event loop error: %s", exc)
        finally:
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        logger.info("Control: stop requested")
        self._shutdown.set()
        if self._ws is not None and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
