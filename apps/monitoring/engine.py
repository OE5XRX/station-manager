"""Alert engine: checks station health and creates/resolves alerts."""

import logging
from datetime import timedelta

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


def _create_alert(station, rule, title, message):
    """Create an alert and return it."""
    alert = Alert.objects.create(
        station=station,
        alert_rule=rule,
        severity=rule.severity,
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
    ).select_related("station", "deployment__firmware_artifact")

    for result in failed_results:
        station = result.station
        if not _has_unresolved_alert(station, AlertRule.AlertType.OTA_FAILED):
            alert = _create_alert(
                station=station,
                rule=rule,
                title=f"OTA deployment failed: {station.name}",
                message=(
                    f"Deployment #{result.deployment_id} "
                    f"({result.deployment.firmware_artifact}) "
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

    if new_alerts:
        logger.info("Alert check complete: %d new alert(s) created.", len(new_alerts))

    _unresolved_cache = None  # Clear cache after run
    return new_alerts
