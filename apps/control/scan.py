"""Server-side scan orchestration (repeater-scanner spec §7).

The agent owns the scan loop; the server owns the list. This module turns a browser
``scan_start`` into the agent frame (via ``apps.repeaters.scan.build_scan_entries``),
keeps the station's ``ActiveScan`` row in step with the scan's lifetime, stops the scan
when the lock changes hands, and pushes a rebuilt list (``scan_update``) whenever one of
the scan's lists is edited.

Only start and stop are audited; individual scan steps never touch the DB here.
All functions are synchronous (consumers wrap them in ``database_sync_to_async``).
"""

import logging
import math
import threading
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import capability_policy, constants
from .models import ActiveScan, StationModule

logger = logging.getLogger(__name__)

# The capabilities every scan step writes: start needs write permission for all of them.
SCAN_CAPABILITIES = ("rx_frequency", "tx_frequency", "rx_tone", "tx_tone")
SCAN_ACTIONS = frozenset({"stop", "skip", "hold", "resume"})

# Bounds mirror the agent's (station_agent/scanner.py): the agent would clamp/refuse anyway,
# but the stored ActiveScan should hold what actually runs.
MAX_ENTRIES = 500
MAX_LISTS = 50
MAX_BAND_MAP = 16
MAX_RADIUS_KM = 1000.0
LISTEN_RANGE_S = (0.2, 60.0)
HANG_RANGE_S = (0.0, 600.0)
DEFAULT_LISTEN_S = 2.0
DEFAULT_HANG_S = 5.0


def agent_group(station_pk):
    return f"control_{station_pk}_agent"


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value, bounds, default):
    if not _is_num(value):
        return default
    lo, hi = bounds
    return float(min(max(value, lo), hi))


def parse_start(msg):
    """Validated scan parameters from a browser ``scan_start`` frame, ``None`` if malformed."""
    radius = msg.get("radius_km")
    if radius is not None and not (_is_num(radius) and 0 < radius <= MAX_RADIUS_KM):
        return None
    list_ids = msg.get("list_ids") or []
    if not isinstance(list_ids, list) or len(list_ids) > MAX_LISTS:
        return None
    if not all(type(i) is int for i in list_ids):
        return None
    band_map = msg.get("band_map") or {}
    if not isinstance(band_map, dict) or len(band_map) > MAX_BAND_MAP:
        return None
    clean_map = {}
    for band, slot in band_map.items():
        if not isinstance(band, str) or not (isinstance(slot, str) or type(slot) is int):
            return None
        clean_map[band[:16]] = str(slot)[:64]
    if radius is None and not list_ids:
        return None  # no source ticked
    return {
        "radius_km": float(radius) if radius is not None else None,
        "list_ids": list(dict.fromkeys(list_ids)),  # dedupe, keep order
        "band_map": clean_map,
        "listen_s": _clamp(msg.get("listen_s"), LISTEN_RANGE_S, DEFAULT_LISTEN_S),
        "hang_s": _clamp(msg.get("hang_s"), HANG_RANGE_S, DEFAULT_HANG_S),
    }


def _wire_slot(slot):
    """Agent frames carry the slot as int (StationModule.slot is a string)."""
    return int(slot) if isinstance(slot, str) and slot.isdigit() else slot


def _wire_entries(entries):
    return [{**e, "slot": _wire_slot(e["slot"])} for e in entries[:MAX_ENTRIES]]


def _frame(mtype, **fields):
    return {"v": constants.PROTOCOL_VERSION, "type": mtype, **fields}


def _build(station, user, params):
    from apps.repeaters.scan import build_scan_entries

    return build_scan_entries(
        station,
        user,
        radius_km=params["radius_km"],
        list_ids=params["list_ids"],
        band_map=params["band_map"],
    )


def _may_write_all(user, module_types):
    return all(
        capability_policy.can_write(user, cap, module_type)
        for cap in SCAN_CAPABILITIES
        for module_type in module_types
    )


def _audit(station, user, message):
    from apps.stations.models import StationAuditLog

    StationAuditLog.log(station=station, event_type="control_command", message=message, user=user)


def start_scan(station, user, msg):
    """``(error_code, None)`` or ``(None, scan_start agent frame)``.

    On success the station's ``ActiveScan`` row is (re)written and the start audited; a
    scan already running is replaced (the agent stops it with reason ``replaced``)."""
    from apps.repeaters.scan import NoCoordinates

    params = parse_start(msg)
    if params is None:
        return "invalid_scan", None
    # Name-level policy first: refuse without building anything.
    if not _may_write_all(user, [None]):
        return "forbidden", None
    try:
        entries = _build(station, user, params)
    except NoCoordinates:
        return "no_coordinates", None
    if not entries:
        return "empty_scan_list", None
    module_types = set(
        StationModule.objects.filter(
            station=station, module_id__in={e["module"] for e in entries}
        ).values_list("type", flat=True)
    )
    if not _may_write_all(user, module_types or [None]):
        return "forbidden", None

    scan_id = uuid.uuid4().hex
    with transaction.atomic():
        previous = ActiveScan.objects.select_for_update().filter(station=station).first()
        if previous is not None:
            _audit(station, previous.user, "scan stopped (replaced)")
            previous.delete()
        ActiveScan.objects.create(station=station, user=user, scan_id=scan_id, **params)
        _audit(station, user, f"{user.username} started scan ({len(entries)} entries)")
    return None, _frame(
        "scan_start",
        scan_id=scan_id,
        entries=_wire_entries(entries),
        listen_s=params["listen_s"],
        hang_s=params["hang_s"],
    )


def current_scan_id(station):
    return ActiveScan.objects.filter(station=station).values_list("scan_id", flat=True).first()


def end_scan(station, reason, scan_id=None):
    """The agent reported the scan stopped (or the agent is gone): drop the row + audit.

    ``scan_id`` guards against a late ``stopped`` of a replaced scan deleting its
    successor. Returns True when a row was removed."""
    with transaction.atomic():
        rows = (
            ActiveScan.objects.select_for_update().select_related("user").filter(station=station)
        )
        if scan_id is not None:
            rows = rows.filter(scan_id=scan_id)
        row = rows.first()
        if row is None:
            return False
        row.delete()
        _audit(station, row.user, f"scan stopped ({str(reason)[:32]})")
    return True


def stop_active_scan(station, reason, *, unless_user=None):
    """Stop the station's scan from the server side: delete the row, audit, and send the
    agent ``scan_control stop``. Called on lock release/transfer/preempt/expiry and on an
    operator stop. ``unless_user``: keep a scan owned by that user (preempting oneself).

    Returns the stopped ``scan_id`` or ``None`` if nothing was running."""
    with transaction.atomic():
        row = (
            ActiveScan.objects.select_for_update()
            .select_related("user")
            .filter(station=station)
            .first()
        )
        if row is None or (unless_user is not None and row.user_id == unless_user.id):
            return None
        row.delete()
        _audit(station, row.user, f"scan stopped ({reason})")
        frame = _frame("scan_control", scan_id=row.scan_id, action="stop")
        transaction.on_commit(lambda: _send_to_agent(station.pk, frame))
    return row.scan_id


def scan_counts_as_activity(station):
    """A running scan keeps the lock from idling out (spec §7.5)."""
    return ActiveScan.objects.filter(station=station).exists()


def _send_to_agent(station_pk, frame):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            agent_group(station_pk), {"type": "control.to_agent", "frame": frame}
        )
    except Exception:
        logger.exception("scan: sending %s to agent failed", frame.get("type"))


# -- list edits -> scan_update ------------------------------------------------

_pending = threading.local()


def push_scan_updates(list_ids):
    """Rebuild every running scan that uses one of ``list_ids`` and push it to its agent.

    An empty rebuilt list is pushed as-is: the agent then stops with ``empty_scan_list``."""
    from apps.repeaters.scan import NoCoordinates

    list_ids = set(list_ids)
    for row in ActiveScan.objects.select_related("station", "user"):
        if not list_ids.intersection(row.list_ids or []):
            continue
        params = {
            "radius_km": row.radius_km,
            "list_ids": row.list_ids,
            "band_map": row.band_map or {},
        }
        try:
            entries = _build(row.station, row.user, params)
        except NoCoordinates:
            entries = []
        except Exception:
            logger.exception("scan: rebuilding scan %s failed", row.scan_id)
            continue
        _send_to_agent(
            row.station_id,
            _frame("scan_update", scan_id=row.scan_id, entries=_wire_entries(entries)),
        )


def _flush_pending():
    ids = getattr(_pending, "ids", None)
    _pending.ids = set()
    if not ids:
        return
    try:
        push_scan_updates(ids)
    except Exception:
        logger.exception("scan: pushing scan updates failed")


def _list_changed(list_id):
    """Coalesce: a cascade delete fires one signal per entry, but one push per commit
    suffices. The flush is registered once per transaction — while it is still queued
    in ``run_on_commit`` ids join the pending set; otherwise (new transaction, or the
    previous one rolled back and dropped its callback) the set starts fresh, so ids of
    a rolled-back edit are never pushed by a later, unrelated commit."""
    if list_id is None:
        return
    conn = transaction.get_connection()
    queued = any(func is _flush_pending for _sids, func, _robust in conn.run_on_commit)
    if not queued:
        _pending.ids = set()
    _pending.ids.add(list_id)
    if not queued:
        transaction.on_commit(_flush_pending)


def _connect_signals():
    from apps.repeaters.models import ScanList, ScanListEntry

    @receiver(post_save, sender=ScanListEntry, weak=False, dispatch_uid="scan_entry_saved")
    @receiver(post_delete, sender=ScanListEntry, weak=False, dispatch_uid="scan_entry_deleted")
    def _entry_changed(sender, instance, **kwargs):
        _list_changed(instance.scan_list_id)

    @receiver(post_delete, sender=ScanList, weak=False, dispatch_uid="scan_list_deleted")
    def _scan_list_deleted(sender, instance, **kwargs):
        _list_changed(instance.pk)
