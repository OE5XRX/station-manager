"""Pure, synchronous registry operations for StationModule.

No async / no I/O beyond the ORM — call from consumers via
``database_sync_to_async`` and directly from unit tests.
"""

from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
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


# Inventory module entry ``status`` of a slot the agent paused for a firmware flash.
MODULE_STATUS_UPDATING = "updating"


def _apply_updating_entry(station, slot, module_id, mod, now):
    """Mark a paused (being-flashed) module updating + online; leave its persisted
    identity, descriptor and last_state untouched. Only a module the server has
    never seen is created from the entry, so the panel can still render it."""
    identity = mod.get("identity") or {}
    cap_descriptor = mod.get("capabilities", []) or []
    raw_state = mod.get("state", {}) or {}
    StationModule.objects.update_or_create(
        station=station,
        slot=slot,
        module_id=module_id,
        # ``last_seen`` refreshes with every paused-slot report, so a long flash never
        # runs the stale-module TTL down (see _prune_expired).
        defaults={
            "online": True,
            "last_seen": now,
            "offline_with_station": False,
            "firmware_updating": True,
        },
        create_defaults={
            "type": identity.get("type", ""),
            "model": identity.get("model", ""),
            "version": identity.get("version", ""),
            "capability_descriptor": cap_descriptor,
            "last_state": {
                k: v for k, v in raw_state.items() if is_setting_cap(cap_descriptor, k)
            },
            "online": True,
            "last_seen": now,
            "firmware_updating": True,
        },
    )


@transaction.atomic
def apply_inventory(station, slots):
    """Upsert all reported modules; soft-offline every module not reported.

    Stale rows are cleaned up as part of a non-empty snapshot: a tracked module that
    moved to another slot loses its old row (settings carried over, see
    :func:`_carry_over_moved`), and offline rows past the TTL are pruned (see
    :func:`_prune_expired`).
    """
    now = timezone.now()
    # Where each tracked module sat (and its settings) BEFORE this snapshot re-links
    # rows — the re-plug carry-over needs it (see _carry_over_moved). Per module: all
    # its keys, plus the most recently seen row (deterministic tie-break on bulk-stamped
    # equal ``last_seen``: newer ``updated_at``, then higher pk).
    before = {}
    for row in StationModule.objects.filter(
        station=station, tracked_module__isnull=False
    ).order_by(F("last_seen").asc(nulls_first=True), "updated_at", "pk"):
        keys = before.get(row.tracked_module_id, (None, set()))[1]
        keys.add((row.slot, row.module_id))
        before[row.tracked_module_id] = (
            (row.slot, row.module_id, dict(row.last_state or {})),
            keys,
        )
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
            if mod.get("status") == MODULE_STATUS_UPDATING:
                _apply_updating_entry(station, slot, module_id, mod, now)
                # The agent paused this slot to flash it: the entry is its last-known
                # snapshot, not a fresh probe. Keep the slot's open assignment (the
                # module is still there) and skip ingest — no identity/version churn
                # and no audits from a mid-flash version.
                tracked_slots.add(slot_str)
                reported.append((slot, module_id))
                continue
            if uid:
                reported_uid_by_slot[slot_str] = uid
            cap_descriptor = mod.get("capabilities", []) or []
            raw_state = mod.get("state", {}) or {}
            filtered_state = {
                k: v for k, v in raw_state.items() if is_setting_cap(cap_descriptor, k)
            }
            # Genuine offline -> online transition of this module (absent before, or
            # soft-offlined by an earlier inventory)? Only that proves it left DFU mode;
            # a re-report while already online may be the agent's debounced last-known
            # entry of a module that in fact still hangs in DFU (no CDC).
            came_online = not StationModule.objects.filter(
                station=station, slot=slot, module_id=module_id, online=True
            ).exists()
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
                    "offline_with_station": False,
                    "firmware_updating": False,
                },
            )
            tracked = ingest_module(
                station, slot, module_id, identity, now=now, came_online=came_online
            )
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

    qs = StationModule.objects.filter(station=station, online=True)
    for slot, module_id in reported:
        qs = qs.exclude(slot=slot, module_id=module_id)
    # A module that drops out was present until now: stamp ``last_seen`` so the stale
    # TTL counts from going offline, not from its last (rare) inventory frame. A module
    # that is no longer reported cannot be mid-flash from the server's view either (its
    # finished event may have been lost with the agent): clear the flag too, the agent
    # re-reports ``updating`` while the slot is still paused.
    qs.update(online=False, last_seen=now, firmware_updating=False)

    # Same guard as reconcile below: ``slots == []`` may be a transient discovery
    # failure and must never delete anything.
    if slots:
        # Modules that went offline together with the station are first seen missing
        # NOW: the outage must not count towards the TTL (a module transiently missed
        # in the reconnect snapshot after a >TTL outage would be pruned at once).
        StationModule.objects.filter(station=station, offline_with_station=True).update(
            last_seen=now, offline_with_station=False
        )
        _carry_over_moved(station, tracked_rows, before)
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


def _carry_over_moved(station, tracked_rows, before):
    """Re-plug: settings follow the physical module (uid) to its new slot.

    ``before`` maps tracked-module id -> ((slot, module_id, last_state) of its most
    recently seen row, all its (slot, module_id) keys) as it was BEFORE
    this snapshot re-linked rows — ``update_or_create`` reuses a (slot, module_id) row
    for whichever module now sits there, so in a rotation/swap the old row is gone or
    belongs to another module by now. For every tracked module reported exactly once,
    at a key where it had no row before:

    - ``last_state``: its previous settings that are still setting caps in the new
      descriptor and have no persisted calibration, overlaid by the freshly reported
      state (the live value wins).
    - ``PersistedCapability`` (what the drift re-apply sends from) is rewritten as a
      whole for all involved keys: each destination gets exactly the mover's rows
      (leftovers of a previous occupant go), a source that nobody moved into ends up
      empty. The consumer runs the re-apply right after this inventory, so calibration
      lost while unplugged is restored in the new slot.

    Independently of a move, rows of a module reported (once) elsewhere are deleted —
    they are the stale panel cards.

    A module being flashed (``firmware_updating`` row, i.e. a paused slot reported as
    ``updating`` or a started-but-unfinished flash) is never part of a move: its row
    is not a stale card, its slot's calibration is neither moved away nor overwritten,
    and a tracked module that has such a row is not treated as re-plugged.
    """
    from apps.stations.models import StationAuditLog

    counts = Counter(tracked.id for _, tracked in tracked_rows)
    reported_pks = {sm.pk for sm, _ in tracked_rows}
    flashing_keys = set()
    flashing_modules = set()
    for slot, module_id, tracked_id in StationModule.objects.filter(
        station=station, firmware_updating=True
    ).values_list("slot", "module_id", "tracked_module_id"):
        flashing_keys.add((slot, module_id))
        if tracked_id is not None:
            flashing_modules.add(tracked_id)
    moves = []
    for sm, tracked in tracked_rows:
        if tracked.id not in before or counts[tracked.id] != 1:
            continue
        if tracked.id in flashing_modules:
            continue
        (slot, module_id, prev_state), keys = before[tracked.id]
        dest = (str(sm.slot), sm.module_id)
        if (str(slot), module_id) in flashing_keys:
            continue
        # Reported where it already had a row: not a move — any other rows are stale
        # duplicates whose settings must not overwrite the live slot's.
        if dest not in keys:
            moves.append((sm, tracked, (slot, module_id), dest, prev_state))
    if moves:
        _move_settings(station, moves)

    # Rows of a module reported (once) elsewhere are stale cards — also when the move
    # itself happened before this snapshot (e.g. before this cleanup was deployed).
    moved_from = {m[1].id: m[2] for m in moves}
    single = [
        (sm, tracked)
        for sm, tracked in tracked_rows
        if counts[tracked.id] == 1 and tracked.id not in flashing_modules
    ]
    stale_by_module = {}
    for row in (
        StationModule.objects.filter(
            station=station, tracked_module_id__in=[t.id for _, t in single]
        )
        .exclude(pk__in=reported_pks)
        .exclude(firmware_updating=True)
    ):
        stale_by_module.setdefault(row.tracked_module_id, []).append(row)
    for sm, tracked in single:
        stale = stale_by_module.get(tracked.id, [])
        removed = [f"{row.slot}/{row.module_id}" for row in stale]
        src = moved_from.get(tracked.id)
        if not removed and src is None:
            continue
        StationModule.objects.filter(pk__in=[row.pk for row in stale]).delete()
        if src is not None:
            message = (
                f"Control panel: module {tracked.uid} moved from slot {src[0]} to slot "
                f"{sm.slot}; settings carried over."
            )
            if removed:
                message += f" Stale entry {', '.join(removed)} removed."
        else:
            message = (
                f"Control panel: removed stale entry {', '.join(removed)} of module "
                f"{tracked.uid} (now in slot {sm.slot})."
            )
        StationAuditLog.log(
            station=station,
            module=tracked,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=message,
        )


def _move_settings(station, moves):
    """Rewrite calibration + merge ``last_state`` for ``moves`` (see _carry_over_moved)."""
    calibration = {}
    for row in PersistedCapability.objects.filter(station=station):
        calibration.setdefault((row.slot, row.module_id), []).append(row)
    involved = {m[2] for m in moves} | {m[3] for m in moves}
    PersistedCapability.objects.filter(
        pk__in=[row.pk for key in involved for row in calibration.get(key, [])]
    ).delete()
    PersistedCapability.objects.bulk_create(
        [
            PersistedCapability(
                station=station,
                slot=dest[0],
                module_id=dest[1],
                capability=row.capability,
                value=row.value,
                updated_by_id=row.updated_by_id,
            )
            for _, _, src, dest, _ in moves
            for row in calibration.get(src, [])
        ]
    )

    for sm, _, src, _, prev_state in moves:
        # Persisted caps are NOT carried over: the re-apply restores them, and a copied
        # value would make the deferred retry (which diffs against last_state) see no
        # drift and skip the restore.
        persisted = {row.capability for row in calibration.get(src, [])}
        merged = {
            k: v
            for k, v in (prev_state or {}).items()
            if k not in persisted and is_setting_cap(sm.capability_descriptor, k)
        }
        merged.update(sm.last_state or {})
        if merged != sm.last_state:
            sm.last_state = merged
            sm.save(update_fields=["last_state", "updated_at"])


def _prune_expired(station, now):
    """Delete offline rows not seen for ``CONTROL_STALE_MODULE_TTL_SECONDS``.

    Rows never seen (``last_seen`` NULL) age by ``updated_at``. Persisted calibration
    is slot-keyed and kept: a module returning to the same slot gets it re-applied.
    A module being flashed (``firmware_updating``) is never pruned.
    """
    from apps.stations.models import StationAuditLog

    ttl = getattr(settings, "CONTROL_STALE_MODULE_TTL_SECONDS", DEFAULT_STALE_MODULE_TTL_SECONDS)
    cutoff = now - timedelta(seconds=ttl)
    expired = StationModule.objects.filter(
        station=station, online=False, firmware_updating=False
    ).filter(Q(last_seen__lt=cutoff) | Q(last_seen__isnull=True, updated_at__lt=cutoff))
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


# Agent ``event`` names that toggle StationModule.firmware_updating.
FIRMWARE_UPDATING_EVENTS = {
    "firmware_update_started": True,
    "firmware_update_finished": False,
}


# ``firmware_update_finished`` result of a cycle that ended before anything was flashed
# (download/checksum failure, agent restart before flashing, agent error). Every other
# result (success / rolled_back incl. DFU_STUCK / rejected) means the flash ran.
FIRMWARE_RESULT_NOT_FLASHED = "failed"


def set_firmware_updating(station, slot, module_id, value, result=None):
    """Set the firmware-updating flag of one module (agent start/finish event).
    ``result`` is the ``firmware_update_finished`` detail's result (None if absent).
    Unknown or malformed addresses are a no-op."""
    if slot is None or not isinstance(module_id, str):
        return
    qs = StationModule.objects.filter(station=station, slot=str(slot), module_id=module_id)
    if value:
        qs.update(firmware_updating=True)
        return
    if result != FIRMWARE_RESULT_NOT_FLASHED:
        # Leaving the updating state after a flash: the module rebooted / re-enumerates,
        # so its presence is unconfirmed until the post-resume inventory (the agent
        # forces that emit) re-reports it. Mark it soft-offline, so that real identity
        # counts as a genuine came-back (clears a DFU-stuck notice) — whereas a module
        # still hanging in DFU stays unreported (agent forget_slot) and offline. Rows
        # that were not updating keep their state.
        # ``last_seen`` is stamped like any online→offline drop, so the stale TTL
        # counts from the end of the flash.
        qs.filter(firmware_updating=True).update(
            firmware_updating=False, online=False, last_seen=timezone.now()
        )
    # A pre-flash failure leaves the module untouched (it never left its app): keep
    # ``online`` as is — a fast cycle may finish before any ``updating`` inventory.
    qs.update(firmware_updating=False)


def mark_station_offline(station):
    # Only the online→offline transition: modules were present until the station
    # dropped and are flagged so the next non-empty snapshot restarts their TTL (the
    # outage does not count). Already-offline rows keep their TTL running.
    # Clear firmware_updating as well (on every row): a ``firmware_update_finished``
    # sent while the agent was disconnected is dropped. On reconnect the agent
    # re-reports ``updating`` for a slot that is still paused.
    StationModule.objects.filter(station=station, online=True).update(
        online=False,
        last_seen=timezone.now(),
        offline_with_station=True,
        firmware_updating=False,
    )
    # Stamp last_seen too: a row whose flash started while soft-offline loses its prune
    # immunity here, so its TTL restarts now (else the first reconnect snapshot prunes it).
    StationModule.objects.filter(station=station, firmware_updating=True).update(
        firmware_updating=False, last_seen=timezone.now()
    )
