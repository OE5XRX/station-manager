"""Alert engine: checks station health and creates/resolves alerts."""

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.deployments.models import DeploymentResult
from apps.stations.models import Station, StationInventory

from .models import Alert, AlertRule

logger = logging.getLogger(__name__)

OFFLINE_THRESHOLD = timedelta(minutes=5)
OTA_CHECK_WINDOW = timedelta(minutes=5)


def _get_active_rule(alert_type):
    """Return the active AlertRule for a given type, or None."""
    try:
        return AlertRule.objects.get(alert_type=alert_type, is_active=True)
    except AlertRule.DoesNotExist:
        return None


_unresolved_cache = None


def _build_unresolved_cache():
    """Pre-fetch all unresolved alerts into a set for O(1) lookups."""
    global _unresolved_cache
    _unresolved_cache = set(
        Alert.objects.filter(is_resolved=False).values_list("station_id", "alert_rule__alert_type")
    )


def _has_unresolved_alert(station, alert_type):
    """Check if an unresolved alert already exists (uses pre-fetched cache)."""
    if _unresolved_cache is not None:
        return (station.id, alert_type) in _unresolved_cache
    return Alert.objects.filter(
        station=station,
        alert_rule__alert_type=alert_type,
        is_resolved=False,
    ).exists()


def _create_alert(station, rule, title, message, severity=None):
    """Create an alert and return it.

    severity: override the rule's default severity (Alert.Severity value).
    When None (the default), rule.severity is used.
    """
    alert = Alert.objects.create(
        station=station,
        alert_rule=rule,
        severity=severity if severity is not None else rule.severity,
        title=title,
        message=message,
    )
    logger.info("Alert created: %s for station %s", title, station.name)
    return alert


def _auto_resolve(alert_type, station=None):
    """Resolve all unresolved alerts of a given type, optionally filtered by station."""
    qs = Alert.objects.filter(
        alert_rule__alert_type=alert_type,
        is_resolved=False,
    )
    if station is not None:
        qs = qs.filter(station=station)
    now = timezone.now()
    count = qs.update(is_resolved=True, resolved_at=now)
    if count:
        logger.info(
            "Auto-resolved %d alert(s) of type %s%s",
            count,
            alert_type,
            f" for station {station.name}" if station else "",
        )
    return count


REBOOT_CHECK_WINDOW = timedelta(minutes=10)
UNEXPECTED_REBOOT_REASONS = {"crash", "watchdog", "undervoltage", "unknown"}


def _check_unexpected_reboot():
    """Alert on reboots whose reason is not clean/ota_rollback (event alert, per-reboot dedup)."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.UNEXPECTED_REBOOT)
    if rule is None:
        return []

    window_start = timezone.now() - REBOOT_CHECK_WINDOW
    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        last_reboot_at__isnull=False,
        last_reboot_at__gte=window_start,
        last_reboot_reason__in=UNEXPECTED_REBOOT_REASONS,
    )
    for tel in qs:
        already = Alert.objects.filter(
            station=tel.station,
            alert_rule__alert_type=AlertRule.AlertType.UNEXPECTED_REBOOT,
            created_at__gte=tel.last_reboot_at,
        ).exists()
        if already:
            continue
        alert = _create_alert(
            station=tel.station,
            rule=rule,
            title=f"Unexpected reboot: {tel.last_reboot_reason}",
            message=(
                f"Station {tel.station.name} rebooted unexpectedly "
                f"(reason: {tel.last_reboot_reason}, boot #{tel.boot_count})."
            ),
        )
        new_alerts.append(alert)
    return new_alerts


def _check_power_warning():
    """Alert on undervoltage/throttling. Severity critical if happening now, else warning."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.POWER_WARNING)
    if rule is None:
        return []

    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        Q(undervoltage_occurred=True) | Q(throttled_occurred=True)
    )
    for tel in qs:
        is_now = bool(tel.undervoltage_now) or bool(tel.throttled_now)
        if _has_unresolved_alert(tel.station, AlertRule.AlertType.POWER_WARNING):
            continue
        severity = Alert.Severity.CRITICAL if is_now else Alert.Severity.WARNING
        what = "Undervoltage" if tel.undervoltage_occurred else "Throttling"
        alert = _create_alert(
            station=tel.station,
            rule=rule,
            title=f"Power warning: {what}",
            message=(f"Station {tel.station.name}: {what} {'ongoing' if is_now else 'occurred'}."),
            severity=severity,
        )
        new_alerts.append(alert)

    # Auto-resolve power alerts for stations where nothing is active or has occurred.
    cleared_ids = list(
        StationTelemetry.objects.filter(
            undervoltage_now=False,
            throttled_now=False,
            undervoltage_occurred=False,
            throttled_occurred=False,
        ).values_list("station_id", flat=True)
    )
    if cleared_ids:
        now = timezone.now()
        Alert.objects.filter(
            alert_rule__alert_type=AlertRule.AlertType.POWER_WARNING,
            is_resolved=False,
            station_id__in=cleared_ids,
        ).update(is_resolved=True, resolved_at=now)

    return new_alerts


def _check_storage_health():
    """Alert on eMMC/SD wear (pre-eol / life-time) or I/O errors."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.STORAGE_HEALTH)
    if rule is None:
        return []

    new_alerts = []
    for tel in StationTelemetry.objects.select_related("station"):
        urgent = tel.worst_pre_eol == "urgent"
        warning = tel.worst_pre_eol == "warning"
        high_wear = (tel.worst_life_time_pct or 0) >= rule.threshold
        io_errors = (tel.io_error_count or 0) > 0
        if not (urgent or warning or high_wear or io_errors):
            continue
        if _has_unresolved_alert(tel.station, AlertRule.AlertType.STORAGE_HEALTH):
            continue
        severity = Alert.Severity.CRITICAL if (urgent or high_wear) else Alert.Severity.WARNING
        reasons = []
        if urgent or warning:
            reasons.append(f"PRE_EOL={tel.worst_pre_eol}")
        if high_wear:
            reasons.append(f"life={tel.worst_life_time_pct}%")
        if io_errors:
            reasons.append(f"{tel.io_error_count} I/O errors")
        alert = _create_alert(
            station=tel.station,
            rule=rule,
            title="Storage health warning",
            message=f"Station {tel.station.name}: {', '.join(reasons)}.",
            severity=severity,
        )
        new_alerts.append(alert)
    return new_alerts


def _check_station_offline():
    """Check for stations that have gone offline (no heartbeat for >5 min)."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.STATION_OFFLINE)
    if not rule:
        return new_alerts

    cutoff = timezone.now() - OFFLINE_THRESHOLD
    stale_stations = Station.objects.filter(
        last_seen__lt=cutoff,
    ).exclude(status=Station.Status.OFFLINE)

    for station in stale_stations:
        if not _has_unresolved_alert(station, AlertRule.AlertType.STATION_OFFLINE):
            alert = _create_alert(
                station=station,
                rule=rule,
                title=f"Station offline: {station.name}",
                message=(
                    f"Station {station.name} has not sent a heartbeat "
                    f"for more than {OFFLINE_THRESHOLD}. "
                    f"Last seen: {station.last_seen}."
                ),
            )
            new_alerts.append(alert)

    # Auto-resolve: stations that are back online
    online_stations = Station.objects.filter(
        last_seen__gte=cutoff,
    )
    for station in online_stations:
        _auto_resolve(AlertRule.AlertType.STATION_OFFLINE, station=station)

    return new_alerts


def _check_cpu_temperature():
    """Check CPU temperature from station inventory data."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.CPU_TEMPERATURE)
    if not rule:
        return new_alerts

    for inventory in StationInventory.objects.select_related("station").all():
        cpu_data = inventory.data.get("cpu", {})
        temp = cpu_data.get("temperature_c")
        if temp is None:
            continue

        station = inventory.station
        if temp >= rule.threshold:
            if not _has_unresolved_alert(station, AlertRule.AlertType.CPU_TEMPERATURE):
                alert = _create_alert(
                    station=station,
                    rule=rule,
                    title=f"High CPU temperature: {station.name}",
                    message=(
                        f"CPU temperature on {station.name} is {temp}C, "
                        f"exceeding threshold of {rule.threshold}C."
                    ),
                )
                new_alerts.append(alert)
        else:
            _auto_resolve(AlertRule.AlertType.CPU_TEMPERATURE, station=station)

    return new_alerts


def _check_disk_usage():
    """Check disk usage from station inventory data."""
    new_alerts = []
    warning_rule = _get_active_rule(AlertRule.AlertType.DISK_WARNING)
    critical_rule = _get_active_rule(AlertRule.AlertType.DISK_CRITICAL)

    if not warning_rule and not critical_rule:
        return new_alerts

    for inventory in StationInventory.objects.select_related("station").all():
        disks = inventory.data.get("disk", [])
        if not isinstance(disks, list):
            continue

        station = inventory.station
        max_usage = 0.0
        for disk in disks:
            usage = disk.get("usage_percent", 0.0)
            if usage > max_usage:
                max_usage = usage

        # Check critical first (higher priority)
        if critical_rule and max_usage >= critical_rule.threshold:
            if not _has_unresolved_alert(station, AlertRule.AlertType.DISK_CRITICAL):
                alert = _create_alert(
                    station=station,
                    rule=critical_rule,
                    title=f"Disk critical: {station.name}",
                    message=(
                        f"Disk usage on {station.name} is {max_usage}%, "
                        f"exceeding critical threshold of {critical_rule.threshold}%."
                    ),
                )
                new_alerts.append(alert)
        elif critical_rule:
            _auto_resolve(AlertRule.AlertType.DISK_CRITICAL, station=station)

        if warning_rule and max_usage >= warning_rule.threshold:
            if not _has_unresolved_alert(station, AlertRule.AlertType.DISK_WARNING):
                alert = _create_alert(
                    station=station,
                    rule=warning_rule,
                    title=f"Disk warning: {station.name}",
                    message=(
                        f"Disk usage on {station.name} is {max_usage}%, "
                        f"exceeding warning threshold of {warning_rule.threshold}%."
                    ),
                )
                new_alerts.append(alert)
        elif warning_rule:
            _auto_resolve(AlertRule.AlertType.DISK_WARNING, station=station)

    return new_alerts


def _check_ram_usage():
    """Check RAM usage from station inventory data."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.RAM_CRITICAL)
    if not rule:
        return new_alerts

    for inventory in StationInventory.objects.select_related("station").all():
        ram_data = inventory.data.get("ram", {})
        usage = ram_data.get("usage_percent")
        if usage is None:
            continue

        station = inventory.station
        if usage >= rule.threshold:
            if not _has_unresolved_alert(station, AlertRule.AlertType.RAM_CRITICAL):
                alert = _create_alert(
                    station=station,
                    rule=rule,
                    title=f"High RAM usage: {station.name}",
                    message=(
                        f"RAM usage on {station.name} is {usage}%, "
                        f"exceeding threshold of {rule.threshold}%."
                    ),
                )
                new_alerts.append(alert)
        else:
            _auto_resolve(AlertRule.AlertType.RAM_CRITICAL, station=station)

    return new_alerts


def _check_ota_failed():
    """Check for recent failed/rolled-back OTA deployments."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.OTA_FAILED)
    if not rule:
        return new_alerts

    cutoff = timezone.now() - OTA_CHECK_WINDOW
    failed_results = DeploymentResult.objects.filter(
        status__in=[DeploymentResult.Status.FAILED, DeploymentResult.Status.ROLLED_BACK],
        completed_at__gte=cutoff,
    ).select_related("station", "deployment__image_release")

    for result in failed_results:
        station = result.station
        if not _has_unresolved_alert(station, AlertRule.AlertType.OTA_FAILED):
            alert = _create_alert(
                station=station,
                rule=rule,
                title=f"OTA deployment failed: {station.name}",
                message=(
                    f"Deployment #{result.deployment_id} "
                    f"({result.deployment.image_release}) "
                    f"on {station.name} has {result.get_status_display().lower()}. "
                    f"Error: {result.error_message or 'No details available.'}"
                ),
            )
            new_alerts.append(alert)

    return new_alerts


def check_alerts():
    """Run all alert checks and return a list of newly created alerts."""
    global _unresolved_cache
    _build_unresolved_cache()

    new_alerts = []
    new_alerts.extend(_check_station_offline())
    new_alerts.extend(_check_cpu_temperature())
    new_alerts.extend(_check_disk_usage())
    new_alerts.extend(_check_ram_usage())
    new_alerts.extend(_check_ota_failed())
    new_alerts.extend(_check_unexpected_reboot())
    new_alerts.extend(_check_power_warning())
    new_alerts.extend(_check_storage_health())

    if new_alerts:
        logger.info("Alert check complete: %d new alert(s) created.", len(new_alerts))

    _unresolved_cache = None  # Clear cache after run
    return new_alerts
