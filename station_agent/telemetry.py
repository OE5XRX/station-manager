"""Assemble the heartbeat telemetry block from feature-detected collectors."""

import logging

from station_agent import bootinfo, inventory, power, storage

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
    return out


def collect_telemetry(config) -> dict:
    """Build the telemetry dict; each block is independently optional."""
    result = {}
    boot = _safe("boot", lambda: bootinfo.detect_boot(
        config.state_dir, _bootloader_or_none(config)))
    if boot:
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
