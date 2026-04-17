"""Background tasks for station status management."""

import logging
from datetime import timedelta

from django.utils import timezone

from .models import Station, StationAuditLog

logger = logging.getLogger(__name__)

OFFLINE_THRESHOLD = timedelta(minutes=2)


def mark_stale_stations_offline():
    """Mark stations as offline if they haven't sent a heartbeat recently."""
    cutoff = timezone.now() - OFFLINE_THRESHOLD
    stale = Station.objects.filter(
        status=Station.Status.ONLINE,
        last_seen__lt=cutoff,
    )
    count = 0
    for station in stale:
        station.status = Station.Status.OFFLINE
        station.save(update_fields=["status", "updated_at"])
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATUS_CHANGE,
            message=f"Station went offline (no heartbeat for >{OFFLINE_THRESHOLD}).",
            changes={"status": {"old": "online", "new": "offline"}},
        )
        count += 1
    if count:
        logger.info(
            "Marked %d station(s) as offline (no heartbeat for >%s)",
            count,
            OFFLINE_THRESHOLD,
        )
    return count
