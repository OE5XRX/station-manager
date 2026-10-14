"""Reference-counted telemetry subscriptions across browser connections.

The agent keeps ONE subscription per (slot, module) for the whole station
(``station_agent.broker``): an ``unsubscribe`` stops telemetry for every
viewer, and a ``subscribe`` replaces the poll interval. A viewer (an Applicant
watching, or the page teardown of anyone closing the panel) must therefore not
reach the agent with those directly. ControlConsumer routes them through here:

* ``subscribe`` is always forwarded (additive at the agent, and it re-arms the
  agent after a reconnect, when browsers re-subscribe on the fresh inventory),
  but with a server-chosen interval: the FASTEST clamped interval any current
  subscriber of that (slot, module) asked for, so nobody can slow others down.
* ``unsubscribe`` / disconnect forward only the capabilities whose count went
  1 -> 0.

Pure sync DB functions (callers wrap them in ``database_sync_to_async``); the
counts live in the DB so they are worker-safe, mirroring
``apps.audio.subscriptions``.
"""

from django.db import transaction
from django.db.models import Min

from .models import TelemetrySubscription

INTERVAL_MIN_MS = 250  # control-logic.js subscribeInterval floor
INTERVAL_MAX_MS = 10_000
INTERVAL_DEFAULT_MS = 1_000  # the agent's default when none/invalid is given
_FIELD_MAX = 64
_MAX_CAPS = 32


def clamp_interval(raw) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw or raw <= 0:
        return INTERVAL_DEFAULT_MS
    return int(min(max(raw, INTERVAL_MIN_MS), INTERVAL_MAX_MS))


def parse_addr(msg) -> tuple[str, str, list[str]] | None:
    """(slot_key, module, capabilities) of a browser frame, or ``None`` if malformed."""
    slot, module = msg.get("slot"), msg.get("module")
    if not (type(slot) is int or isinstance(slot, str)) or not isinstance(module, str):
        return None
    slot_key = str(slot)
    if not slot_key or len(slot_key) > _FIELD_MAX or not module or len(module) > _FIELD_MAX:
        return None
    raw_caps = msg.get("capabilities")
    if not isinstance(raw_caps, list):
        return None
    caps = []
    for cap in raw_caps:
        if isinstance(cap, str) and 0 < len(cap) <= _FIELD_MAX and cap not in caps:
            caps.append(cap)
    if not caps or len(caps) > _MAX_CAPS:
        return None
    return slot_key, module, caps


def slot_value(slot_key: str):
    """Wire form of a stored slot: numeric slots go back as ints (JS ``slotAddr``)."""
    return int(slot_key) if slot_key.isdigit() else slot_key


def _lock_station(station):
    from apps.stations.models import Station

    # Row lock on the station serializes concurrent count changes across workers.
    Station.objects.select_for_update().filter(pk=station.pk).first()


@transaction.atomic
def subscribe(station, channel_name, slot_key, module, caps, interval_ms) -> int:
    """Record ``channel_name``'s demand; return the interval to forward."""
    _lock_station(station)
    interval_ms = clamp_interval(interval_ms)
    for cap in caps:
        TelemetrySubscription.objects.update_or_create(
            station=station,
            slot=slot_key,
            module=module,
            capability=cap,
            channel_name=channel_name,
            defaults={"interval_ms": interval_ms},
        )
    return TelemetrySubscription.objects.filter(
        station=station, slot=slot_key, module=module
    ).aggregate(m=Min("interval_ms"))["m"]


def _zeroed(station, slot_key, module, caps) -> list[str]:
    still = set(
        TelemetrySubscription.objects.filter(
            station=station, slot=slot_key, module=module, capability__in=caps
        ).values_list("capability", flat=True)
    )
    return [cap for cap in caps if cap not in still]


@transaction.atomic
def unsubscribe(station, channel_name, slot_key, module, caps) -> list[str]:
    """Drop ``channel_name``'s demand; return the capabilities that hit zero.

    Only capabilities this channel actually held can be returned, so an
    unsubscribe for someone else's subscription never reaches the agent.
    """
    _lock_station(station)
    rows = TelemetrySubscription.objects.filter(
        station=station,
        slot=slot_key,
        module=module,
        capability__in=caps,
        channel_name=channel_name,
    )
    held = [cap for cap in caps if cap in set(rows.values_list("capability", flat=True))]
    rows.delete()
    return _zeroed(station, slot_key, module, held)


@transaction.atomic
def drop_channel(station, channel_name) -> list[tuple[str, str, list[str]]]:
    """Remove all of a closed connection's demand; return what hit zero per (slot, module)."""
    _lock_station(station)
    rows = TelemetrySubscription.objects.filter(station=station, channel_name=channel_name)
    held: dict[tuple[str, str], list[str]] = {}
    for slot_key, module, cap in rows.values_list("slot", "module", "capability"):
        held.setdefault((slot_key, module), []).append(cap)
    rows.delete()
    result = []
    for (slot_key, module), caps in sorted(held.items()):
        zeroed = _zeroed(station, slot_key, module, sorted(caps))
        if zeroed:
            result.append((slot_key, module, zeroed))
    return result
