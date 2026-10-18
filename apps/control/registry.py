"""Pure, synchronous registry operations for StationModule.

No async / no I/O beyond the ORM — call from consumers via
``database_sync_to_async`` and directly from unit tests.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module, reconcile_station
from apps.module_firmware.models import Module

from .models import PersistedCapability, StationModule

DEFAULT_STALE_MODULE_TTL_SECONDS = 48 * 3600


def is_setting_cap(descriptor, cap_name):
    """True iff ``cap_name`` is a *setting* capability in ``descriptor``.

    Unknown caps return False so they are treated as ephemeral (telemetry)
    and never persisted into ``last_state``.
    """
    for cap in descriptor or []:
        if cap.get("name") == cap_name:
            return cap.get("kind") == "setting"
    return False


@transaction.atomic
def apply_inventory(station, slots):
    """Upsert all reported modules; soft-offline every module not reported.

    Stale rows are cleaned up as part of a non-empty snapshot: a tracked module that
    moved to another slot loses its old row (settings carried over, see
    :func:`_carry_over_moved`), and offline rows past the TTL are pruned (see
    :func:`_prune_expired`).
    """
    now = timezone.now()
    # No module online yet = first snapshot after the station was offline (see below).
    # ``Max`` over all rows = the moment ``mark_station_offline`` stamped them.
    reconnect_ts = None
    if not StationModule.objects.filter(station=station, online=True).exists():
        reconnect_ts = StationModule.objects.filter(station=station).aggregate(
            ts=Max("last_seen")
        )["ts"]
    reported = []
    # (row, tracked Module) for every reported, uid-tracked module — re-plug candidates.
    tracked_rows = []
    tracked_slots = set()
    # Maps slot (str) -> reported uid for any uid-bearing entry, tracked or
    # rejected. Lets reconcile preserve an assignment when the SAME physical uid
    # is still reported in its slot but the entry was rejected (type mismatch),
    # while still releasing it when a different / no-uid module took the slot.
    reported_uid_by_slot = {}
    for slot_entry in slots or []:
        slot = slot_entry.get("slot")
        for mod in slot_entry.get("modules", []) or []:
            module_id = mod.get("module")
            if slot is None or module_id is None:
                continue
            identity = mod.get("identity") or {}
            uid = identity.get("uid")
            slot_str = str(slot)
            if uid:
                reported_uid_by_slot[slot_str] = uid
            cap_descriptor = mod.get("capabilities", []) or []
            raw_state = mod.get("state", {}) or {}
            filtered_state = {
                k: v for k, v in raw_state.items() if is_setting_cap(cap_descriptor, k)
            }
            sm, _ = StationModule.objects.update_or_create(
                station=station,
                slot=slot,
                module_id=module_id,
                defaults={
                    "type": identity.get("type", ""),
                    "model": identity.get("model", ""),
                    "version": identity.get("version", ""),
                    "capability_descriptor": cap_descriptor,
                    "last_state": filtered_state,
                    "online": True,
                    "last_seen": now,
                },
            )
            tracked = ingest_module(station, slot, module_id, identity, now=now)
            if tracked is not None:
                if sm.tracked_module_id != tracked.id:
                    sm.tracked_module = tracked
                    sm.save(update_fields=["tracked_module"])
                tracked_slots.add(slot_str)
                tracked_rows.append((sm, tracked))
            else:
                # Rejected entry (legacy no-UID / unknown type / type mismatch).
                # Keep the existing link ONLY if the SAME uid as the currently
                # linked module is reported — i.e. a type-mismatch of the tracked
                # module itself, where the module is still physically present.
                # Otherwise the slot no longer holds that module: clear the link
                # so it agrees with the assignment history reconcile will update.
                linked_uid = None
                if sm.tracked_module_id:
                    linked_uid = (
                        Module.objects.filter(pk=sm.tracked_module_id)
                        .values_list("uid", flat=True)
                        .first()
                    )
                keep = bool(uid) and uid == linked_uid
                if not keep and sm.tracked_module_id is not None:
                    sm.tracked_module = None
                    sm.save(update_fields=["tracked_module"])
            reported.append((slot, module_id))

    reported_pks = {sm.pk for sm, _ in tracked_rows}
    qs = StationModule.objects.filter(station=station, online=True)
    for slot, module_id in reported:
        qs = qs.exclude(slot=slot, module_id=module_id)
    # A module that drops out was present until now: stamp ``last_seen`` so the stale
    # TTL counts from going offline, not from its last (rare) inventory frame.
    qs.update(online=False, last_seen=now)
    if reconnect_ts is not None:
        # Modules present when the station dropped are first seen missing *now*; the
        # outage itself must not count towards the TTL (a module transiently missed
        # in the reconnect snapshot after a >TTL outage would be pruned at once).
        # Rows already offline before the outage keep their older last_seen. (If no
        # module was online at the drop, the newest offline rows get one extra TTL.)
        unreported = StationModule.objects.filter(station=station, last_seen=reconnect_ts)
        for slot, module_id in reported:
            unreported = unreported.exclude(slot=slot, module_id=module_id)
        unreported.update(last_seen=now)

    # Same guard as reconcile below: ``slots == []`` may be a transient discovery
    # failure and must never delete anything.
    if slots:
        for sm, tracked in tracked_rows:
            _carry_over_moved(station, sm, tracked, reported, reported_pks)
        _prune_expired(station, now)

    # Reconcile tracked-module assignments against the snapshot. Guarded on
    # ``slots`` being non-empty (NOT ``reported``): a responsive-but-empty slot
    # list still reports a real snapshot and must close pulled modules, whereas
    # ``slots == []`` is indistinguishable from a transient discovery failure and
    # must NOT tear everything down. Full completeness-aware reconciliation is
    # Teilbereich C's job.
    if slots:
        reconcile_station(
            station, tracked_slots, now=now, reported_uid_by_slot=reported_uid_by_slot
        )


@transaction.atomic
def apply_state(station, slot, module_id, values):
    """Merge only *setting* caps of ``values`` into the module's last_state."""
    try:
        module = StationModule.objects.select_for_update().get(
            station=station, slot=slot, module_id=module_id
        )
    except StationModule.DoesNotExist:
        return
    descriptor = module.capability_descriptor
    changed = False
    for cap_name, value in (values or {}).items():
        if is_setting_cap(descriptor, cap_name):
            module.last_state[cap_name] = value
            changed = True
    if changed:
        # updated_at is auto_now, but Django does NOT auto-add auto_now fields
        # to an explicit update_fields — list it so the settings-change
        # timestamp actually advances on the incremental state path.
        module.save(update_fields=["last_state", "updated_at"])


def _carry_over_moved(station, sm, tracked, reported, reported_pks):
    """Re-plug: ``tracked`` (same uid) is now reported in ``sm``'s slot — delete its
    rows in other, no-longer-reported slots of this station and carry their settings over.

    Merge rule for ``last_state``: the old row's settings that are still setting caps in
    the new descriptor, overlaid by the freshly reported state (the module's live value
    always wins). Persisted calibration (``PersistedCapability``, the source the drift
    re-apply sends from) moves to the new slot and replaces any leftover rows there —
    those belonged to the slot's previous occupant, not to this physical module. The
    consumer runs the re-apply right after this inventory, so a module that lost its
    calibration while unplugged gets it back in its new slot.

    Known gap: a *swap* (A and B exchange slots in one snapshot) reports both rows, so
    nothing is carried over and calibration stays slot-bound as before this cleanup.
    """
    reported_keys = {(str(slot), module_id) for slot, module_id in reported}
    olds = [
        old
        for old in StationModule.objects.filter(station=station, tracked_module=tracked)
        .exclude(pk__in=reported_pks)
        .order_by("last_seen")
        if (old.slot, old.module_id) not in reported_keys
    ]
    if not olds:
        return
    merged = {}
    for old in olds:  # oldest first: the most recently seen row wins a conflict
        merged.update(
            {
                k: v
                for k, v in (old.last_state or {}).items()
                if is_setting_cap(sm.capability_descriptor, k)
            }
        )
    merged.update(sm.last_state or {})
    if merged != sm.last_state:
        sm.last_state = merged
        sm.save(update_fields=["last_state", "updated_at"])

    from apps.stations.models import StationAuditLog

    # Everything persisted at the new slot belonged to its previous occupant — drop it
    # all (not just colliding caps), even if the moved module brings no calibration.
    PersistedCapability.objects.filter(
        station=station, slot=sm.slot, module_id=sm.module_id
    ).delete()
    for old in olds:
        moved = PersistedCapability.objects.filter(
            station=station, slot=old.slot, module_id=old.module_id
        )
        for row in moved:  # several old rows: the most recently seen one wins
            PersistedCapability.objects.filter(
                station=station, slot=sm.slot, module_id=sm.module_id, capability=row.capability
            ).delete()
        moved.update(slot=sm.slot, module_id=sm.module_id)
        old.delete()
        StationAuditLog.log(
            station=station,
            module=tracked,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=(
                f"Control panel: module {tracked.uid} moved from slot {old.slot} to "
                f"slot {sm.slot}; stale entry {old.slot}/{old.module_id} removed, "
                "settings carried over."
            ),
        )


def _prune_expired(station, now):
    """Delete offline rows not seen for ``CONTROL_STALE_MODULE_TTL_SECONDS``.

    Rows never seen (``last_seen`` NULL) age by ``updated_at``. Persisted calibration
    is slot-keyed and kept: a module returning to the same slot gets it re-applied.
    """
    from apps.stations.models import StationAuditLog

    ttl = getattr(settings, "CONTROL_STALE_MODULE_TTL_SECONDS", DEFAULT_STALE_MODULE_TTL_SECONDS)
    cutoff = now - timedelta(seconds=ttl)
    expired = StationModule.objects.filter(station=station, online=False).filter(
        Q(last_seen__lt=cutoff) | Q(last_seen__isnull=True, updated_at__lt=cutoff)
    )
    for old in expired:
        old.delete()
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATUS_CHANGE,
            message=(
                f"Control panel: removed stale module entry {old.slot}/{old.module_id} "
                f"(offline, not seen for more than {ttl / 3600:g} h)."
            ),
        )


def mark_station_offline(station):
    # Stamp ``last_seen`` only on the online→offline transition: modules were present
    # until the station dropped; already-offline rows keep their TTL running.
    StationModule.objects.filter(station=station, online=True).update(
        online=False, last_seen=timezone.now()
    )
