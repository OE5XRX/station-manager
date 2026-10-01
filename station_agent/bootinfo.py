# station_agent/bootinfo.py
"""Boot / reboot-reason detection — agent-owned per telemetry contract."""

import logging

logger = logging.getLogger(__name__)


def compute_reboot_reason(evidence: dict) -> str:
    """Classify why the previous boot ended, from collected evidence.

    Priority: crash > ota_rollback > watchdog > undervoltage > clean > unknown.
    A clean systemd shutdown overrides a lingering 'undervoltage occurred' bit.
    """
    if evidence.get("pstore_crash"):
        return "crash"
    if evidence.get("ota_rollback"):
        return "ota_rollback"
    if evidence.get("watchdog"):
        return "watchdog"
    if evidence.get("clean_marker"):
        return "clean"
    if evidence.get("undervoltage_occurred"):
        return "undervoltage"
    return "unknown"
