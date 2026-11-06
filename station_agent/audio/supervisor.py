"""AudioSupervisor — keeps the audio channel in step with the audio slots actually present.

Audio slots used to be detected once at agent start. When a module was moved to another
slot, plugged in later, unplugged, or re-enumerated in place (module firmware DFU), the
audio channel kept advertising the stale slot until the agent was restarted (the Web UI's
RX subscription hung in ERROR).

The supervisor owns that lifecycle:

- **Snapshot** = ``{slot: (card index, generation)}`` from the backend
  (``PipeWireRouterBackend.audio_slot_generations``). A changed generation for the same slot
  means the device re-enumerated in place.
- **Trigger** = the control channel's existing re-discovery cycle (``kick()`` is wired as the
  ``ControlClient`` re-scan hook; a module firmware cycle end forces one) — no own poller. The
  scan runs on the supervisor's thread: ``udevadm`` subprocesses must not block the control
  event loop.
- **Debounce**: a changed snapshot is applied only after ``confirm`` identical consecutive
  scans spaced ``settle_s`` apart (re-enumeration flaps briefly); bounded by ``max_rounds``,
  after which the change waits for the next trigger.
- **Apply**: 0→n starts the audio client and registers the audio-router virtual module on the
  control plane; n→0 stops both (the server marks the streams idle on the audio-WS close);
  any other change asks the running client to refresh (re-advertise / rebuild pipelines).
  An unchanged scan still lets the client heal a dead RX pipeline.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT = 5.0


class AudioSupervisor:
    def __init__(
        self,
        backend,
        *,
        client_factory,
        router_factory,
        settle_s: float = 2.0,
        confirm: int = 3,
        max_rounds: int = 6,
        wait=None,
    ):
        self._backend = backend
        self._client_factory = client_factory
        self._router_factory = router_factory
        self._settle_s = settle_s
        self._confirm = max(1, confirm)
        self.max_rounds = max_rounds
        self._shutdown = threading.Event()
        self._kick = threading.Event()
        # Interruptible sleep between confirm scans; returns True once shutdown was requested.
        self._wait = wait or self._shutdown.wait
        # Serialises client/router lifecycle between the supervisor thread and stop().
        self._lock = threading.Lock()
        self._applied: dict = {}
        self._control = None
        self._router = None
        self._client = None
        self._client_thread: threading.Thread | None = None
        self._thread: threading.Thread | None = None

    # --- scanning ----------------------------------------------------------
    def scan(self) -> dict | None:
        """Current ``{slot: generation}`` snapshot, or ``None`` if the backend failed."""
        try:
            generations = getattr(self._backend, "audio_slot_generations", None)
            if generations is not None:
                return dict(generations())
            return {slot: None for slot in self._backend.list_audio_slots()}
        except Exception:  # noqa: BLE001 — detection must never crash the agent
            logger.debug("audio supervisor: slot scan failed", exc_info=True)
            return None

    def initial_scan(self) -> list[int]:
        """Synchronous startup detection. Fail-closed: a failed scan means audio off."""
        snap = self.scan() or {}
        self._applied = snap
        slots = sorted(snap)
        logger.info(
            "audio detect: audio path %s (slots=%s)", "present" if slots else "absent", slots
        )
        if slots:
            self._router = self._router_factory()
        return slots

    @property
    def router_module(self):
        """The audio-router virtual module, or ``None`` while no audio slot is present."""
        return self._router

    # --- lifecycle ---------------------------------------------------------
    def start(self, control) -> None:
        """Start the audio client (if slots were found) and the supervisor thread.

        *control* is the ``ControlClient``; the router module found by :meth:`initial_scan`
        already went in through its constructor, later changes use ``set_virtual_modules``.
        """
        self._control = control
        with self._lock:
            if self._applied:
                self._start_client()
            else:
                logger.info("Audio channel disabled")
        self._thread = threading.Thread(target=self._run, name="audio-supervisor", daemon=True)
        self._thread.start()

    def kick(self) -> None:
        """Thread-safe, non-blocking: re-check the audio slots on the supervisor thread."""
        self._kick.set()

    def stop(self) -> None:
        self._shutdown.set()
        self._kick.set()
        if self._thread is not None:
            self._thread.join(timeout=_JOIN_TIMEOUT)
        with self._lock:
            self._stop_client()

    def slot_active(self, slot: int) -> bool:
        """Cross-thread: delegate to the running audio client (False while audio is off)."""
        client = self._client
        return client is not None and client.slot_active(slot)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            self._kick.wait()
            self._kick.clear()
            if self._shutdown.is_set():
                break
            try:
                self.check()
            except Exception:  # noqa: BLE001 — the supervisor must outlive any one check
                logger.exception("audio supervisor: slot check failed")

    # --- check / apply -----------------------------------------------------
    def check(self) -> bool:
        """Scan once; apply a debounced change. Returns True if a change was applied."""
        snap = self.scan()
        if snap is None:
            return False
        if snap == self._applied:
            self._refresh_client(set())
            return False
        stable, rounds = 1, 0
        while stable < self._confirm:
            if rounds >= self.max_rounds:
                logger.info(
                    "audio supervisor: audio slots still changing after %d scans; "
                    "deferring to the next re-discovery",
                    rounds,
                )
                return False
            if self._wait(self._settle_s):
                return False  # shutting down
            rounds += 1
            nxt = self.scan()
            if nxt is None:
                return False
            if nxt == snap:
                stable += 1
            else:
                snap, stable = nxt, 1
        if snap == self._applied:
            self._refresh_client(set())
            return False  # flapped back to what is applied
        with self._lock:
            if self._shutdown.is_set():
                return False
            self._apply(snap)
        return True

    def _apply(self, snap: dict) -> None:
        old, self._applied = self._applied, snap
        slots = sorted(snap)
        if not slots:
            logger.info("Audio: audio slots %s gone; stopping audio channel", sorted(old))
            self._stop_client()
            self._router = None
            self._set_virtual([])
            return
        if self._client is None:
            logger.info("Audio: audio slots %s appeared; starting audio channel", slots)
            if self._router is None:
                self._router = self._router_factory()
            self._set_virtual([self._router])
            self._start_client()
            return
        rebuild = {s for s in snap if s in old and old[s] != snap[s]}
        logger.info(
            "Audio: audio slots changed %s -> %s (re-enumerated: %s)",
            sorted(old),
            slots,
            sorted(rebuild),
        )
        self._refresh_client(rebuild)

    def _refresh_client(self, rebuild: set) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.update_slots(sorted(self._applied), rebuild)
        except Exception:  # noqa: BLE001
            logger.exception("audio supervisor: client refresh failed")

    def _set_virtual(self, modules: list) -> None:
        setter = getattr(self._control, "set_virtual_modules", None)
        if setter is None:
            return
        try:
            setter(modules)
        except Exception:  # noqa: BLE001
            logger.exception("audio supervisor: updating control virtual modules failed")

    def _start_client(self) -> None:
        logger.info("Audio channel enabled")
        try:
            client = self._client_factory()
        except Exception:  # noqa: BLE001 — a broken audio setup must not take the agent down
            logger.exception("audio supervisor: audio client setup failed; audio stays off")
            return
        thread = threading.Thread(target=client.run, name="audio-client", daemon=True)
        self._client, self._client_thread = client, thread
        thread.start()

    def _stop_client(self) -> None:
        client, thread = self._client, self._client_thread
        self._client = self._client_thread = None
        if client is None:
            return
        try:
            client.stop()
        except Exception:  # noqa: BLE001
            logger.exception("audio supervisor: audio client stop failed")
        if thread is not None:
            thread.join(timeout=_JOIN_TIMEOUT)
