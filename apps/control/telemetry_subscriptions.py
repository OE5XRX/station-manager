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

Only (slot, module) pairs the station actually registered (``StationModule``) are
accepted, capabilities are narrowed to the ones the module describes, and one
connection holds at most :data:`MAX_ROWS_PER_CHANNEL` rows — a ``control.view``
holder cannot grow the table or relay invented addresses to the agent.

Liveness: every row carries ``last_seen``, stamped on subscribe and by the
owning ControlConsumer's periodic re-check (:func:`touch`). A connection that
died without ``disconnect()`` (SIGTERM/crash) stops stamping; its rows are
purged once older than :data:`STALE_AFTER_SECONDS` — before every mutation, on
agent replay and from the agent consumer's sweep tick (:func:`purge_stale`) —
so dead demand neither pins the fastest interval nor gets replayed forever.

Pure sync DB functions (callers wrap them in ``database_sync_to_async``); the
counts live in the DB so they are worker-safe, mirroring
``apps.audio.subscriptions``.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from .constants import PROTOCOL_VERSION
from .models import StationModule, TelemetrySubscription

INTERVAL_MIN_MS = 250  # control-logic.js subscribeInterval floor
INTERVAL_MAX_MS = 10_000
INTERVAL_DEFAULT_MS = 1_000  # the agent's default when none/invalid is given
_FIELD_MAX = 64
_MAX_CAPS = 32
_SLOT_MAX = 2**31 - 1

#: Rows (distinct (slot, module, capability)) one browser connection may hold.
MAX_ROWS_PER_CHANNEL = 64
#: A row not re-stamped for this long belongs to a dead connection. Live viewers
#: re-stamp every ``permissions.PERIODIC_RECHECK_SECONDS`` (60 s): 3x leaves room
#: for a failed re-check tick.
STALE_AFTER_SECONDS = 180.0

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
    rows = _rows(station, slot, module).filter(last_seen__gte=_stale_cutoff())
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


def _stale_cutoff():
    return timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS)


def _stale_rows(station):
    return TelemetrySubscription.objects.filter(station=station, last_seen__lt=_stale_cutoff())


def _purge_stale_locked(station) -> list[dict]:
    """Delete dead connections' rows (caller holds the station lock); return the frames
    that release the demand they held. ``before`` counts the stale rows too: that is
    the demand the agent was last armed with."""
    stale = _stale_rows(station)
    addrs = _addrs(stale)
    if not addrs:
        return []
    befores = {}
    for slot, module in addrs:
        rows = _rows(station, slot, module)
        befores[(slot, module)] = (
            set(rows.values_list("capability", flat=True)),
            rows.aggregate(m=Min("interval_ms"))["m"],
        )
    stale.delete()
    frames = []
    for addr in addrs:
        frames += _transition(*addr, befores[addr], _state(station, *addr))
    return frames


def purge_stale(station) -> list[dict]:
    """Sweep entry point: purge stale rows; return the frames to relay (maybe none)."""
    if not _stale_rows(station).exists():
        return []  # common case: no station lock taken
    with transaction.atomic():
        _lock_station(station)
        return _purge_stale_locked(station)


def touch(station, channel_name) -> None:
    """Liveness stamp for all of ``channel_name``'s rows (connection still open)."""
    TelemetrySubscription.objects.filter(station=station, channel_name=channel_name).update(
        last_seen=timezone.now()
    )


def _described_caps(station, slot, module, caps) -> list[str]:
    """``caps`` narrowed to what the registered module describes; ``[]`` if the
    station never registered (slot, module). A module without a descriptor keeps
    ``caps`` (already bounded by :func:`parse_addr`)."""
    descriptor = (
        StationModule.objects.filter(station=station, slot=str(slot), module_id=module)
        .values_list("capability_descriptor", flat=True)
        .first()
    )
    if descriptor is None:
        return []
    names = {
        c.get("name")
        for c in (descriptor if isinstance(descriptor, list) else [])
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    if not names:
        return list(caps)
    return [c for c in caps if c in names]


def subscribe(station, channel_name, slot, module, caps, interval_ms) -> list[dict]:
    """Record ``channel_name``'s demand; return the frames to relay (maybe none).

    A channel's interval for a capability is fixed by its first subscribe, so
    re-subscribing never wobbles the shared interval. Unregistered addresses /
    undescribed capabilities are dropped before any lock is taken; new rows
    beyond :data:`MAX_ROWS_PER_CHANNEL` are not created.
    """
    caps = _described_caps(station, slot, module, caps)
    if not caps:
        return []
    with transaction.atomic():
        _lock_station(station)
        touch(station, channel_name)  # before the purge: this connection is alive
        frames = _purge_stale_locked(station)
        before = _state(station, slot, module)
        interval_ms = clamp_interval(interval_ms)
        held = TelemetrySubscription.objects.filter(station=station, channel_name=channel_name)
        existing = set(
            held.filter(slot=str(slot), module=module, capability__in=caps).values_list(
                "capability", flat=True
            )
        )
        room = MAX_ROWS_PER_CHANNEL - held.count()
        for cap in caps:
            if cap in existing:
                continue
            if room <= 0:
                break
            TelemetrySubscription.objects.create(
                station=station,
                slot=str(slot),
                module=module,
                capability=cap,
                channel_name=channel_name,
                interval_ms=interval_ms,
            )
            room -= 1
        return frames + _transition(slot, module, before, _state(station, slot, module))


def unsubscribe(station, channel_name, slot, module, caps) -> list[dict]:
    """Drop ``channel_name``'s demand for ``caps``; return the frames to relay.

    Only rows this channel holds are touched, so an unsubscribe for someone
    else's subscription changes nothing and relays nothing (and takes no lock).
    """
    mine = _rows(station, slot, module).filter(capability__in=caps, channel_name=channel_name)
    if not mine.exists():
        return []
    with transaction.atomic():
        _lock_station(station)
        frames = _purge_stale_locked(station)
        before = _state(station, slot, module)
        mine.delete()
        return frames + _transition(slot, module, before, _state(station, slot, module))


@transaction.atomic
def drop_channel(station, channel_name) -> list[dict]:
    """Remove all of a closed connection's demand; return the frames to relay."""
    _lock_station(station)
    frames = _purge_stale_locked(station)
    rows = TelemetrySubscription.objects.filter(station=station, channel_name=channel_name)
    addrs = _addrs(rows)
    befores = {addr: _state(station, *addr) for addr in addrs}
    rows.delete()
    for addr in addrs:
        frames += _transition(*addr, befores[addr], _state(station, *addr))
    return frames


@transaction.atomic
def active_subscriptions(station) -> list[dict]:
    """Subscribe frames for the whole current demand (agent replay after reconnect).

    Stale rows are purged first and never replayed (the fresh agent holds no
    subscriptions, so the purge's release frames are moot)."""
    _lock_station(station)
    _purge_stale_locked(station)
    frames = []
    for slot, module in _addrs(TelemetrySubscription.objects.filter(station=station)):
        caps, interval_ms = _state(station, slot, module)
        if caps:
            frames.append(subscribe_frame(slot, module, sorted(caps), interval_ms))
    return frames
