"""Serial-Contract-Selftest: öffnet das Slot-Control-Device über die echten
Produktionspfade und hexdumpt den Verkehr. Grün nur wenn ein Modul antwortet.
Ehrlichkeits-Regel: an dieser Grenze zählt nur ein grüner Lauf auf echtem CM4."""

import asyncio
import logging
import sys
import time

from station_agent import slot_discovery
from station_agent.broker import Broker

logger = logging.getLogger("station_agent.selftest")


def run_serial(control_path: str, *, timeout: float = 3.0) -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("selftest serial: probing %s (trace on)", control_path)
    modules = slot_discovery.probe_slot(control_path, timeout=timeout, trace=True)
    if not modules:
        logger.error("selftest serial: FAIL — no module described on %s", control_path)
        return 1
    for m in modules:
        logger.info(
            "selftest serial: OK — module %s identity=%s caps=%s",
            m.get("id"),
            m.get("identity"),
            m.get("capabilities"),
        )
    return 0


def _telemetry_caps(entry: dict) -> list[tuple]:
    caps = []
    for module in entry.get("modules", []):
        for cap in module.get("capabilities", []):
            if cap.get("kind") == "telemetry" and isinstance(cap.get("name"), str):
                caps.append((entry.get("slot"), module.get("id"), cap["name"]))
    return caps


def run_control_stability(
    base: str,
    *,
    duration: float = 20.0,
    poll_hz: float = 10.0,
    rescan_s: float = 2.0,
    discover_fn=None,
    transport_factory=None,
    now=time.monotonic,
    sleep=asyncio.sleep,
) -> int:
    """Stress the control line the way the live agent does: an active telemetry poll hammers
    a slot's control serial while re-discovery re-scans it, repeatedly, for ``duration``.

    This is the regression gate for the PR #135 flap: without the broker's control lock
    governing BOTH paths, a re-scan racing a poll tick corrupts the probe's MODULE-LIST and
    the slot vanishes from that scan. The test fails (returns 1) if ANY re-discovery drops a
    slot that was present at the start — the raw, un-debounced signal, so it stays sensitive
    even though the live loop additionally debounces. ``discover_fn`` / ``transport_factory``
    are injectable for a hardware-free unit test; the real run uses the production paths.

    Honesty rule: a green run only counts on real CM4 — the sim pty cannot reproduce the
    CDC-ACM timing that opens the collision window.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Validate the CLI knobs before anything runs: duration<=0 would skip the scan loop and
    # report a false "0 scans, 0 flaps" pass; poll_hz<=0 divides by zero in the interval math
    # (max(1, int(1000/poll_hz))); rescan_s<=0 removes the pacing between scans. Fail closed.
    if duration <= 0 or poll_hz <= 0 or rescan_s <= 0:
        logger.error(
            "selftest control-stability: FAIL — duration, poll_hz and rescan_s must all be > 0 "
            "(got duration=%r, poll_hz=%r, rescan_s=%r)",
            duration,
            poll_hz,
            rescan_s,
        )
        return 1
    disc = discover_fn or (lambda: slot_discovery.discover_slots(base))

    async def scenario() -> int:
        initial = disc()
        expected = sorted(e.get("slot") for e in initial)
        if not expected:
            logger.error("selftest control-stability: FAIL — no slots discovered on %s", base)
            return 1
        sub_targets = [t for e in initial for t in _telemetry_caps(e)]
        if not sub_targets:
            logger.error("selftest control-stability: FAIL — no telemetry caps to poll")
            return 1

        broker = Broker(
            _noop_send,
            transport_factory=transport_factory,
            telemetry_min_floor_ms=1,
            telemetry_default_interval_ms=max(1, int(1000 / poll_hz)),
        )
        broker.set_inventory(initial)
        interval_ms = max(1, int(1000 / poll_hz))
        caps_by_addr: dict = {}
        for slot, module, cap in sub_targets:
            caps_by_addr.setdefault((slot, module), []).append(cap)
        for (slot, module), caps in caps_by_addr.items():
            await broker.handle_subscribe(
                {"slot": slot, "module": module, "capabilities": caps, "interval_ms": interval_ms}
            )

        scans = 0
        flaps = 0
        deadline = now() + duration
        try:
            while now() < deadline:
                result = await broker.rediscover(disc)
                scans += 1
                present = {e.get("slot") for e in result}
                missing = [s for s in expected if s not in present]
                if missing:
                    flaps += 1
                    logger.error(
                        "selftest control-stability: slot(s) %s dropped on scan %d (contention!)",
                        missing,
                        scans,
                    )
                else:
                    broker.set_inventory(result)
                await sleep(rescan_s)
        finally:
            await broker.stop()

        if flaps:
            logger.error(
                "selftest control-stability: FAIL — %d/%d scans dropped a present slot",
                flaps,
                scans,
            )
            return 1
        logger.info(
            "selftest control-stability: OK — %d scans, 0 flaps under %.0f Hz poll load",
            scans,
            poll_hz,
        )
        return 0

    return asyncio.run(scenario())


async def _noop_send(_msg):
    return None
