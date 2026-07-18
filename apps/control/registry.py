"""Pure, synchronous registry operations for StationModule.

No async / no I/O beyond the ORM — call from consumers via
``database_sync_to_async`` and directly from unit tests.
"""

from django.db import transaction
from django.utils import timezone

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
            StationModule.objects.update_or_create(
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
            reported.append((slot, module_id))

    qs = StationModule.objects.filter(station=station, online=True)
    for slot, module_id in reported:
        qs = qs.exclude(slot=slot, module_id=module_id)
    qs.update(online=False)


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
