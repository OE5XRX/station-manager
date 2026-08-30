"""Serial-Contract-Selftest: öffnet das Slot-Control-Device über die echten
Produktionspfade und hexdumpt den Verkehr. Grün nur wenn ein Modul antwortet.
Ehrlichkeits-Regel: an dieser Grenze zählt nur ein grüner Lauf auf echtem CM4."""

import logging
import sys

from station_agent import slot_discovery

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
