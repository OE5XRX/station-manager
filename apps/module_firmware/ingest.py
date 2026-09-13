"""Module inventory ingestion. Pure ORM, synchronous — called from
apps.control.registry.apply_inventory inside its atomic transaction.

Assignment/swap and lifecycle derivation live in _apply_assignment /
_derive_lifecycle; ingest_module wires them in.
"""

import logging

from apps.stations.models import StationAuditLog

from .models import Module, ModuleAssignmentHistory, ModuleType

logger = logging.getLogger(__name__)


def ingest_module(station, slot, module_id, identity, *, now, user=None):
    """Upsert a Module from a self-reported identity dict. Returns the Module
    or None (no UID => legacy path; unknown type => rejected)."""
    identity = identity or {}
    uid = identity.get("uid")
    if not uid:
        return None  # legacy firmware without UID: StationModule-only display

    type_key = identity.get("type") or module_id
    try:
        module_type = ModuleType.objects.get(key=type_key)
    except ModuleType.DoesNotExist:
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"Ignored module with unregistered type '{type_key}' (uid={uid}).",
        )
        logger.warning("ingest: unknown module type %r (uid=%s)", type_key, uid)
        return None

    uid_source = identity.get("uid_source") or Module.UidSource.STM32_UID
    version = identity.get("version", "")

    module, created = Module.objects.get_or_create(
        uid=uid,
        defaults={
            "module_type": module_type,
            "uid_source": uid_source,
            "last_reported_version": version,
            "first_seen": now,
            "last_seen": now,
        },
    )
    if created:
        StationAuditLog.log(
            station=station, module=module,
            event_type=StationAuditLog.EventType.MODULE_DISCOVERED,
            message=f"Module {uid} ({module_type.key}) discovered in {station}/{slot}.",
        )
    else:
        module.last_seen = now
        module.last_reported_version = version
        module.save(update_fields=["last_seen", "last_reported_version", "updated_at"])

    _apply_assignment(module, station, slot, now=now, user=user)
    _derive_lifecycle(module, now=now)
    return module


def _apply_assignment(module, station, slot, *, now, user=None):
    """Ensure exactly one open assignment for (module) at (station, slot).
    Closes any conflicting open rows and opens a new one on change.
    Returns True if the assignment changed."""
    current = module.assignments.filter(to_ts__isnull=True).first()
    if current and current.station_id == station.id and current.slot == slot:
        return False  # unchanged — idempotent

    displaced = False

    # Close this module's open assignment elsewhere.
    if current:
        current.to_ts = now
        current.save(update_fields=["to_ts"])

    # Close whatever other module currently occupies (station, slot).
    occupant = ModuleAssignmentHistory.objects.filter(
        station=station, slot=slot, to_ts__isnull=True
    ).exclude(module=module).first()
    if occupant:
        occupant.to_ts = now
        occupant.save(update_fields=["to_ts"])
        displaced = True

    ModuleAssignmentHistory.objects.create(
        module=module, station=station, slot=slot,
        from_ts=now, reason="auto-swap", created_by=user,
    )

    if displaced:
        StationAuditLog.log(
            station=station, module=module,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=f"Module {module.uid} replaced a module in {station}/{slot}.",
        )
    StationAuditLog.log(
        station=station, module=module,
        event_type=StationAuditLog.EventType.MODULE_ASSIGNMENT_CHANGED,
        message=f"Module {module.uid} assigned to {station}/{slot}.",
    )
    return True


def _derive_lifecycle(module, *, now):
    """Derive lifecycle from assignment state. Never overrides sticky states."""
    if module.lifecycle_status in Module.STICKY_LIFECYCLE:
        return
    has_open = module.assignments.filter(to_ts__isnull=True).exists()
    target = Module.Lifecycle.DEPLOYED if has_open else Module.Lifecycle.READY
    if module.lifecycle_status != target:
        old = module.lifecycle_status
        module.lifecycle_status = target
        module.save(update_fields=["lifecycle_status", "updated_at"])
        StationAuditLog.log(
            module=module,
            event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
            message=f"Lifecycle {old} → {target} for module {module.uid}.",
            changes={"lifecycle_status": {"old": old, "new": target}},
        )
