"""Assemble the heartbeat telemetry block from feature-detected collectors."""

import logging

from station_agent import bootinfo, inventory, power, storage
from station_agent import heartbeat as hb

logger = logging.getLogger(__name__)


def _safe(label, fn):
    """Run a collector; never raise. Returns its result or None on error."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - telemetry must never break heartbeat
        logger.debug("telemetry collector %s failed: %s", label, exc)
        return None


def _collect_slot(config) -> dict:
    from station_agent import bootloader

    out = {}
    try:
        bl = bootloader.get_bootloader(config)
        out["active_slot"] = bootloader.get_active_slot(bl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("active slot probe failed: %s", exc)
        out["active_slot"] = None
    try:
        out["image_version"] = inventory.get_current_version()
    except Exception as exc:  # noqa: BLE001
        logger.debug("image version probe failed: %s", exc)
        out["image_version"] = ""
    # M13: read persisted OTA outcome written by agent._verify_and_commit.
    try:
        ota_result = bootinfo.read_last_ota_result(config.state_dir)
        if ota_result is not None:
            out["last_ota_result"] = ota_result.get("result") or ""
            detail = ota_result.get("detail") or ""
            if detail:
                out["last_ota_detail"] = detail
        else:
            out["last_ota_result"] = None
    except Exception as exc:  # noqa: BLE001
        logger.debug("last_ota_result probe failed: %s", exc)
        out["last_ota_result"] = None
    return out


def collect_telemetry(config) -> dict:
    """Build the telemetry dict; each block is independently optional."""
    result = {}
    boot = _safe(
        "boot", lambda: bootinfo.detect_boot(config.state_dir, _bootloader_or_none(config))
    )
    if boot:
        # M14: include uptime_seconds so ingest can populate StationTelemetry.uptime_seconds.
        # Degrade to omitted if /proc/uptime is unreadable (hb.get_uptime returns 0.0 there,
        # so we skip the 0.0 value to avoid ambiguity with a real 0-second uptime).
        uptime = _safe("uptime", hb.get_uptime)
        if uptime:
            boot = dict(boot, uptime_seconds=uptime)
        result["boot"] = boot
    pwr = _safe("power", power.read_throttle)
    if pwr:
        result["power"] = pwr
    stor = _safe("storage", storage.read_storage_health)
    if stor:
        result["storage"] = stor
    slot = _safe("slot", lambda: _collect_slot(config))
    if slot:
        result["slot"] = slot
    return result


def _bootloader_or_none(config):
    try:
        from station_agent import bootloader

        return bootloader.get_bootloader(config)
    except Exception:  # noqa: BLE001
        return None
