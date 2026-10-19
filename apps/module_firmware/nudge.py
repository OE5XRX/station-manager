"""Nudge station agents to run a module-firmware check right now.

The agent polls ``reconcile/check`` on its own cadence; a target/release change
pushes a ``firmware_check`` frame over the control channel so it reacts at once.
Best effort only: a lost nudge is harmless (the poll still converges)."""

import logging
from collections.abc import Iterable

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

logger = logging.getLogger(__name__)

FIRMWARE_CHECK_FRAME = {"v": 1, "type": "firmware_check"}


def _send(station_ids):
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        for station_id in station_ids:
            try:
                async_to_sync(layer.group_send)(
                    f"control_{station_id}_agent",
                    {"type": "control.to_agent", "frame": dict(FIRMWARE_CHECK_FRAME)},
                )
            except Exception:
                logger.warning(
                    "firmware_check nudge to station %s failed", station_id, exc_info=True
                )
    except Exception:
        logger.warning("firmware_check nudge failed", exc_info=True)


def nudge_stations(station_ids: Iterable[int]) -> None:
    """Send ``firmware_check`` to each (deduplicated) station after commit."""
    ids = sorted({sid for sid in station_ids if sid is not None})
    if not ids:
        return
    transaction.on_commit(lambda: _send(ids))


def _send_lock_refresh(station_ids):
    from apps.control import lock
    from apps.stations.models import Station

    try:
        layer = get_channel_layer()
        if layer is None:
            return
        for station in Station.objects.filter(pk__in=station_ids):
            try:
                status = lock.status_with_drain(station)
                async_to_sync(layer.group_send)(
                    f"control_{station.pk}", {"type": "control.lock", "lock": status}
                )
            except Exception:
                logger.warning("lock refresh to station %s failed", station.pk, exc_info=True)
    except Exception:
        logger.warning("lock refresh failed", exc_info=True)


def refresh_lock_status(station_ids: Iterable[int]) -> None:
    """Re-broadcast ``control.lock`` (fresh drain payload) to each station's open
    panels after commit — for drain changes that send no ``firmware_check`` nudge
    (e.g. a target delete/archive ending the drain)."""
    ids = sorted({sid for sid in station_ids if sid is not None})
    if not ids:
        return
    transaction.on_commit(lambda: _send_lock_refresh(ids))
