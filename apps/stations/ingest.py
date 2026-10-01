"""Persist agent-reported telemetry into StationTelemetry + audit reboots."""

import logging

from django.utils import timezone

from apps.stations.models import StationAuditLog, StationTelemetry

logger = logging.getLogger(__name__)

_PRE_EOL_RANK = {"normal": 0, "n/a": 0, "": 0, "warning": 1, "urgent": 2}


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _worst_storage(storage: dict):
    """Return (worst_life_time_pct, worst_pre_eol, io_error_count)."""
    devices = storage.get("devices") if isinstance(storage, dict) else None
    if not isinstance(devices, list):
        return None, "", 0
    worst_life = None
    worst_eol = ""
    io_errors = 0
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        for key in ("life_time_a_pct", "life_time_b_pct"):
            val = dev.get(key)
            if isinstance(val, int):
                worst_life = val if worst_life is None else max(worst_life, val)
        eol = dev.get("pre_eol") or ""
        if _PRE_EOL_RANK.get(eol, 0) > _PRE_EOL_RANK.get(worst_eol, 0):
            worst_eol = eol
        if isinstance(dev.get("io_error_count"), int):
            io_errors += dev["io_error_count"]
    return worst_life, worst_eol, io_errors


def ingest_telemetry(station, telemetry):
    """Upsert StationTelemetry; log a REBOOT audit event on boot_id change."""
    if not isinstance(telemetry, dict):
        logger.warning("Ignoring non-dict telemetry for station %s", station.pk)
        return None

    boot = _as_dict(telemetry.get("boot"))
    powerd = _as_dict(telemetry.get("power"))
    slot = _as_dict(telemetry.get("slot"))
    storaged = _as_dict(telemetry.get("storage"))

    tel, _created = StationTelemetry.objects.get_or_create(station=station)
    prev_boot_id = tel.boot_id
    new_boot_id = str(boot.get("boot_id") or "")

    reboot_detected = bool(prev_boot_id) and bool(new_boot_id) and new_boot_id != prev_boot_id

    tel.data = telemetry
    tel.boot_id = new_boot_id or prev_boot_id
    if isinstance(boot.get("boot_count"), int):
        tel.boot_count = boot["boot_count"]
    # M7: on a boot_id transition, never inherit the old reason — a new boot with
    # an omitted reason should default to "unknown", not silently keep "clean" (or
    # any prior reason) which would mask unexpected-reboot alerting.
    if reboot_detected:
        tel.last_reboot_reason = str(boot.get("reboot_reason") or "unknown")
    else:
        tel.last_reboot_reason = str(boot.get("reboot_reason") or tel.last_reboot_reason or "")
    if isinstance(boot.get("uptime_seconds"), (int, float)):
        tel.uptime_seconds = float(boot["uptime_seconds"])

    tel.undervoltage_now = powerd.get("undervoltage_now")
    tel.undervoltage_occurred = powerd.get("undervoltage_occurred")
    tel.throttled_now = powerd.get("throttled_now")
    tel.throttled_occurred = powerd.get("throttled_occurred")

    tel.active_slot = str(slot.get("active_slot") or "")[:1]
    tel.image_version = str(slot.get("image_version") or "")[:100]
    tel.last_ota_result = str(slot.get("last_ota_result") or "")[:16]

    worst_life, worst_eol, io_errors = _worst_storage(storaged)
    tel.worst_life_time_pct = worst_life
    tel.worst_pre_eol = worst_eol
    tel.io_error_count = io_errors

    if reboot_detected:
        tel.last_reboot_at = timezone.now()
        # M2: the dmesg ring buffer resets at reboot, so prior I/O error counts no
        # longer reflect the new boot's device state.  Reset the baseline so
        # fresh errors in the new boot are not suppressed by a stale prior value.
        tel.alerted_io_error_count = 0

    tel.save()

    if reboot_detected:
        StationAuditLog.objects.create(
            station=station,
            event_type=StationAuditLog.EventType.REBOOT,
            message=f"Station rebooted (reason: {tel.last_reboot_reason or 'unknown'})",
            changes={"boot_id": {"old": prev_boot_id, "new": new_boot_id}},
        )
    return tel
