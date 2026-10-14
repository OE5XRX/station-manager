"""Reference-counted telemetry subscriptions across browser connections.

The agent keeps ONE subscription per (slot, module) for the whole station
(``station_agent.broker``): an ``unsubscribe`` stops telemetry for every
viewer, a ``subscribe`` replaces the poll interval and restarts the poll task
(an immediate poll on the single serial line). A viewer therefore never
reaches the agent directly; ControlConsumer routes subscribe/unsubscribe
through here and relays only *state changes* of the station-wide demand:

* a capability going 0 -> 1 (subscribe) or 1 -> 0 (unsubscribe);
* the effective interval changing — the FASTEST clamped interval any current
  subscriber of that (slot, module) asked for (faster subscriber joins, or the
  fastest one leaves and the interval relaxes).

Repeated identical subscribes change nothing and relay nothing. After an agent
(re)connect, AgentControlConsumer replays :func:`active_subscriptions` on the
fresh inventory, so browsers' re-subscribes need not reach the agent.

Addresses are parsed exactly like the agent (``broker._valid_addr``): ``slot``
must be an ``int`` (not bool), ``module`` a ``str``, capabilities the ``str``
items of a list. Relayed frames are built only from the parsed address
(:func:`subscribe_frame` / :func:`unsubscribe_frame`), never from the
browser's frame.

Pure sync DB functions (callers wrap them in ``database_sync_to_async``); the
counts live in the DB so they are worker-safe, mirroring
``apps.audio.subscriptions``.
"""

from django.db import transaction
from django.db.models import Min

from .constants import PROTOCOL_VERSION
from .models import TelemetrySubscription

INTERVAL_MIN_MS = 250  # control-logic.js subscribeInterval floor
INTERVAL_MAX_MS = 10_000
INTERVAL_DEFAULT_MS = 1_000  # the agent's default when none/invalid is given
_FIELD_MAX = 64
_MAX_CAPS = 32
_SLOT_MAX = 2**31 - 1

#: Per browser connection: at most FRAME_BUDGET subscribe/unsubscribe frames per
#: FRAME_WINDOW_SECONDS are processed (excess dropped). A real panel sends one
#: subscribe per module per inventory and one unsubscribe per module on close.
FRAME_BUDGET = 40
FRAME_WINDOW_SECONDS = 10.0


def clamp_interval(raw) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw or raw <= 0:
        return INTERVAL_DEFAULT_MS
    return int(min(max(raw, INTERVAL_MIN_MS), INTERVAL_MAX_MS))


def parse_addr(msg) -> tuple[int, str, list[str]] | None:
    """(slot, module, capabilities) parsed with the agent's rules, or ``None``."""
    if msg.get("v") != PROTOCOL_VERSION:
        return None  # the agent drops a frame with a missing/mismatched envelope
    slot, module = msg.get("slot"), msg.get("module")
    if type(slot) is not int or not -_SLOT_MAX <= slot <= _SLOT_MAX:
        return None
    if not isinstance(module, str) or not 0 < len(module) <= _FIELD_MAX:
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
    return slot, module, caps


def subscribe_frame(slot: int, module: str, caps, interval_ms: int) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": "subscribe",
        "slot": slot,
        "module": module,
        "capabilities": list(caps),
        "interval_ms": interval_ms,
    }


def unsubscribe_frame(slot: int, module: str, caps) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": "unsubscribe",
        "slot": slot,
        "module": module,
        "capabilities": list(caps),
    }


def _lock_station(station):
    from apps.stations.models import Station

    # Row lock on the station serializes concurrent count changes across workers.
    Station.objects.select_for_update().filter(pk=station.pk).first()


def _rows(station, slot: int, module: str):
    # slot is stored canonically as str(int): the only form parse_addr admits.
    return TelemetrySubscription.objects.filter(station=station, slot=str(slot), module=module)


def _state(station, slot, module) -> tuple[set[str], int | None]:
    rows = _rows(station, slot, module)
    caps = set(rows.values_list("capability", flat=True))
    return caps, rows.aggregate(m=Min("interval_ms"))["m"]


def _addrs(rows) -> list[tuple[int, str]]:
    addrs = set()
    for slot, module in rows.values_list("slot", "module"):
        try:
            addrs.add((int(slot), module))
        except ValueError:
            continue  # never written by parse_addr; ignore defensively
    return sorted(addrs)


def _transition(slot, module, before, after) -> list[dict]:
    """Agent frames turning demand ``before`` into ``after`` (each: (caps, interval))."""
    (caps_before, iv_before), (caps_after, iv_after) = before, after
    frames = []
    gone = sorted(caps_before - caps_after)
    if gone:
        frames.append(unsubscribe_frame(slot, module, gone))
    if not caps_after:
        return frames
    new = sorted(caps_after - caps_before)
    if iv_after != iv_before:
        frames.append(subscribe_frame(slot, module, sorted(caps_after), iv_after))
    elif new:
        frames.append(subscribe_frame(slot, module, new, iv_after))
    return frames


@transaction.atomic
def subscribe(station, channel_name, slot, module, caps, interval_ms) -> list[dict]:
    """Record ``channel_name``'s demand; return the frames to relay (maybe none).

    A channel's interval for a capability is fixed by its first subscribe, so
    re-subscribing never wobbles the shared interval.
    """
    _lock_station(station)
    before = _state(station, slot, module)
    interval_ms = clamp_interval(interval_ms)
    for cap in caps:
        TelemetrySubscription.objects.get_or_create(
            station=station,
            slot=str(slot),
            module=module,
            capability=cap,
            channel_name=channel_name,
            defaults={"interval_ms": interval_ms},
        )
    return _transition(slot, module, before, _state(station, slot, module))


@transaction.atomic
def unsubscribe(station, channel_name, slot, module, caps) -> list[dict]:
    """Drop ``channel_name``'s demand for ``caps``; return the frames to relay.

    Only rows this channel holds are touched, so an unsubscribe for someone
    else's subscription changes nothing and relays nothing.
    """
    _lock_station(station)
    before = _state(station, slot, module)
    _rows(station, slot, module).filter(capability__in=caps, channel_name=channel_name).delete()
    return _transition(slot, module, before, _state(station, slot, module))


@transaction.atomic
def drop_channel(station, channel_name) -> list[dict]:
    """Remove all of a closed connection's demand; return the frames to relay."""
    _lock_station(station)
    rows = TelemetrySubscription.objects.filter(station=station, channel_name=channel_name)
    addrs = _addrs(rows)
    befores = {addr: _state(station, *addr) for addr in addrs}
    rows.delete()
    frames = []
    for addr in addrs:
        frames += _transition(*addr, befores[addr], _state(station, *addr))
    return frames


def active_subscriptions(station) -> list[dict]:
    """Subscribe frames for the whole current demand (agent replay after reconnect)."""
    frames = []
    for slot, module in _addrs(TelemetrySubscription.objects.filter(station=station)):
        caps, interval_ms = _state(station, slot, module)
        if caps:
            frames.append(subscribe_frame(slot, module, sorted(caps), interval_ms))
    return frames
