"""Why a station may not run a passive service right now (``None`` = eligible)."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from apps.control import lock as control_lock
from apps.control.models import ControlLock
from apps.stations.models import Station

from .selection import pick_slot

REASONS = (
    "disabled",
    "not_opted_in",
    "offline",
    "agent_too_old",
    "updating",
    "draining",
    "lock_held",
    "idle_wait",
    "no_module",
)

DECODERS_FEATURE = "decoders"


def ineligible_reason(
    station, cfg, *, now, agent_connected: bool, agent_features: set[str]
) -> str | None:
    if not cfg.enabled:
        return "disabled"
    station_cfg = getattr(station, "passive_config", None)
    if station_cfg is None or not station_cfg.enabled or station_cfg.igate_call is None:
        return "not_opted_in"
    if not agent_connected:
        return "offline"
    if DECODERS_FEATURE not in agent_features:
        return "agent_too_old"
    if station.status == Station.Status.UPDATING:
        return "updating"
    if control_lock.is_draining(station):
        return "draining"
    lock = ControlLock.objects.filter(station=station, scope="station").first()
    if lock is not None:
        if lock.holder_id is not None:
            return "lock_held"
        if lock.released_at is not None and now - lock.released_at < timedelta(
            minutes=cfg.idle_minutes
        ):
            return "idle_wait"
    try:
        freq = Decimal(str(cfg.profile.get("frequency")))
    except (InvalidOperation, TypeError):
        return "no_module"
    if pick_slot(station, freq) is None:
        return "no_module"
    return None
