"""Pure, synchronous registry operations for StationModule.

No async / no I/O beyond the ORM — call from consumers via
``database_sync_to_async`` and directly from unit tests.
"""

from django.db import transaction
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module, reconcile_station

from .models import StationModule


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
    """Upsert all reported modules; soft-offline every module not reported."""
    now = timezone.now()
    reported = []
    tracked_slots = set()
    for slot_entry in slots or []:
        slot = slot_entry.get("slot")
        for mod in slot_entry.get("modules", []) or []:
            module_id = mod.get("module")
            if slot is None or module_id is None:
                continue
            identity = mod.get("identity") or {}
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
            tracked_pk = tracked.id if tracked else None
            if sm.tracked_module_id != tracked_pk:
                sm.tracked_module = tracked
                sm.save(update_fields=["tracked_module"])
            if tracked is not None:
                # slot is stored as CharField; normalize for the reconcile set.
                tracked_slots.add(str(slot))
            reported.append((slot, module_id))

    qs = StationModule.objects.filter(station=station, online=True)
    for slot, module_id in reported:
        qs = qs.exclude(slot=slot, module_id=module_id)
    qs.update(online=False)

    # Reconcile tracked-module assignments against slots that currently hold a
    # linked Module. Only on a non-empty snapshot: an empty inventory is
    # indistinguishable from a transient discovery failure, and tearing down
    # every assignment on a failed probe would be worse than a briefly-stale
    # assignment. Full completeness-aware reconciliation is Teilbereich C's job;
    # here we only close slots that dropped from a snapshot that DID report
    # something. Legacy/unknown entries are excluded (tracked_slots only holds
    # slots with a linked Module), so a legacy module replacing a tracked one
    # still releases the old assignment.
    if reported:
        reconcile_station(station, tracked_slots, now=now)


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


def mark_station_offline(station):
    StationModule.objects.filter(station=station).update(online=False)
